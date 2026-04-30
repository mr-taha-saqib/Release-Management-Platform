"""
mcp/processor.py -- Layer 2: Processor

Responsibility:
  * Accept a ContextStore from Layer 1.
  * Run all analytical computations on the raw data.
  * Store computed metrics back into context.metrics.
  * Return the updated ContextStore so Layer 3 can use it.

Computations performed (all arithmetic, no text generation):
  - Task breakdown counts (done / in-progress / blocked / to-do)
  - Completion percentage
  - Time variance from completed tasks (actual vs estimated)
  - Remaining hours (raw and variance-adjusted)
  - Daily team capacity
  - Days to finish, projected finish date, deadline buffer
  - Risk level (LOW / MEDIUM / HIGH / CRITICAL)
  - Per-member load ratio and load status
  - Blocked task details
  - Risk factors and priority recommendations (as data, not prose)

Isolation guarantee:
  The Processor only reads from context.raw_data.
  It never reads from the filesystem or any network.
  Every output value is a direct arithmetic result of input data.

Usage:
    ctx = ContextStore.load("flask")
    ctx = Processor().process(ctx)
    print(ctx.metrics["risk_level"])
"""

from __future__ import annotations
from datetime import date, datetime, timedelta
from mcp.context import ContextStore

TODAY     = date.today()
_MAX_DAYS = 9999  # used as infinity when team capacity is zero


class Processor:
    """
    Stateless analyser.  Call process(context) to populate context.metrics.

    All computations are reproducible: same input data always produces
    the same output metrics.

    This class has no instance state — every computation reads from
    the ContextStore passed to process() and writes back into it.
    """

    def process(self, context: ContextStore) -> ContextStore:
        """
        Analyse the data in context.raw_data and populate context.metrics.

        This is the only public method. It runs all 11 analytical steps
        in sequence and stores every computed value in context.metrics
        as a flat dictionary of scalars and lists.

        Parameters
        ----------
        context : ContextStore
            A store loaded by ContextStore.load(), not yet processed.
            Must contain raw_data with keys: project, tasks, team.

        Returns
        -------
        ContextStore
            The same object with context.metrics now populated.
            Returning it allows chaining: ctx = Processor().process(ctx).

        Raises
        ------
        ValueError
            If project["deadline"] is not in YYYY-MM-DD format.
        """
        data    = context.raw_data
        project = data["project"]
        tasks   = data["tasks"]
        team    = data["team"]

        # ── 1. Task breakdown ─────────────────────────────────────────────────
        # Group tasks by their status field. These four statuses are the only
        # valid values expected from both local JSON and Jira live data.
        done    = [t for t in tasks if t.get("status") == "Done"]
        in_prog = [t for t in tasks if t.get("status") == "In Progress"]
        blocked = [t for t in tasks if t.get("status") == "Blocked"]
        todo    = [t for t in tasks if t.get("status") == "To Do"]
        total   = len(tasks)
        pct     = round(len(done) / total * 100, 1) if total else 0.0

        # ── 2. Time variance (completed tasks only) ───────────────────────────
        # Variance measures how accurate the original estimates were.
        # Formula per task: (actual - estimated) / estimated
        # Positive variance means tasks took longer than expected (over-run).
        # Negative variance means tasks finished early.
        # We skip tasks where actual_time is None (Jira live tasks with no
        # time logged) or where estimated_time is 0 (no estimate entered).
        variances = []
        for t in done:
            est = t.get("estimated_time") or 0
            act = t.get("actual_time")          # may be None for live Jira tasks
            if est > 0 and act is not None and act > 0:
                variances.append((act - est) / est)
        avg_var = sum(variances) / len(variances) if variances else 0.0
        # Floor at -90%: a task cannot "save" more than 90% of its estimate.
        # This prevents unrealistically optimistic adjustments.
        avg_var = max(-0.9, avg_var)

        # ── 3. Remaining hours ────────────────────────────────────────────────
        # Raw remaining = sum of (estimated - actual) for all unfinished tasks.
        # We use `or 0` guards because live Jira tasks may have None fields.
        # max(0.0, ...) prevents negative values if actual > estimated.
        remaining = sum(
            max(0.0, (t.get("estimated_time") or 0) - (t.get("actual_time") or 0))
            for t in in_prog + blocked + todo
        )
        # Variance-adjusted remaining: inflate or deflate based on historical accuracy.
        # Example: if past tasks ran 20% over, we expect remaining work to also run 20% over.
        adjusted = max(0.0, remaining * (1 + avg_var))

        # ── 4. Team capacity (hours/day) ──────────────────────────────────────
        # Each member's weekly capacity is divided by 5 to get hours per working day.
        # Defaults to 40h/week (8h/day) if not specified in the team data.
        daily_cap = sum(m.get("capacity_hours_per_week", 40) / 5 for m in team)

        # ── 5. Timeline forecast ──────────────────────────────────────────────
        # How many working days until the adjusted work is complete at current capacity.
        days_left = adjusted / daily_cap if daily_cap > 0 else float(_MAX_DAYS)
        proj_fin  = TODAY + timedelta(days=round(days_left))

        # Parse the deadline from the project data (must be YYYY-MM-DD)
        try:
            deadline = datetime.strptime(project["deadline"], "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(
                f"deadline '{project['deadline']}' must be YYYY-MM-DD format."
            )

        # Buffer = days between projected finish and the hard deadline.
        # Positive buffer means we finish before the deadline (healthy).
        # Negative buffer means we are projected to miss the deadline.
        buf      = (deadline - proj_fin).days
        on_track = proj_fin <= deadline

        # ── 6. Risk level ─────────────────────────────────────────────────────
        # A simple decision tree converts numeric signals into one of four
        # categorical risk levels. Evaluated from worst to best.
        n_blocked = len(blocked)
        if buf < 0 or n_blocked >= 3:
            # Already past deadline, or 3+ tasks completely stalled
            risk = "CRITICAL"
        elif n_blocked >= 1 or buf < 7 or avg_var > 0.25:
            # At least one blocker, less than a week of buffer, or severe over-runs
            risk = "HIGH"
        elif avg_var > 0.10 or buf < 21:
            # Mild over-runs or tight deadline (less than 3 weeks of buffer)
            risk = "MEDIUM"
        else:
            risk = "LOW"

        # ── 7. Resource analysis ──────────────────────────────────────────────
        # Load ratio compares each team member's assigned hours against the
        # team average. A ratio of 1.0 means perfectly average load.
        # >1.4 = overloaded (carrying 40% more than average)
        # <0.6 = underutilised (carrying 40% less than average)
        task_map     = {t["id"]: t for t in tasks}  # lookup by task ID
        total_est    = sum(t.get("estimated_time", 0) for t in tasks)
        avg_per_head = total_est / len(team) if team else 0.0

        team_analysis = []
        for member in team:
            ids           = member.get("assigned_tasks", [])
            mbr_tasks     = [task_map[i] for i in ids if i in task_map]
            mbr_hours     = sum(t.get("estimated_time", 0) for t in mbr_tasks)
            mbr_done      = sum(1 for t in mbr_tasks if t.get("status") == "Done")
            mbr_blocked   = sum(1 for t in mbr_tasks if t.get("status") == "Blocked")
            load_ratio    = round(mbr_hours / avg_per_head, 2) if avg_per_head > 0 else 1.0

            if load_ratio > 1.4:
                load_status = "overloaded"
            elif load_ratio < 0.6:
                load_status = "underutilised"
            else:
                load_status = "balanced"

            team_analysis.append({
                "name":              member.get("name", "Unknown"),
                "role":              member.get("role", "Contributor"),
                "tasks_assigned":    len(mbr_tasks),
                "tasks_done":        mbr_done,
                "tasks_blocked":     mbr_blocked,
                "estimated_hours":   mbr_hours,
                "capacity_per_week": member.get("capacity_hours_per_week", 40),
                "load_ratio":        load_ratio,
                "load_status":       load_status,
            })

        # ── 8. Blocked task details ───────────────────────────────────────────
        # Extract the fields the Responder needs to display per blocked task.
        # _real tracks whether the task came from a real API (Jira/GitHub) or
        # was a synthetic supplemental record — used for labelling in answers.
        blocked_details = [
            {
                "id":          t.get("id", "?"),
                "title":       t.get("title", "Untitled"),
                "assigned_to": t.get("assigned_to", "Unassigned"),
                "hours":       t.get("estimated_time", 0),
                "priority":    t.get("priority", "Medium"),
                "real":        t.get("_real", None),   # track data origin
            }
            for t in blocked
        ]

        # ── 9. Risk factors (structured data, not generated text) ─────────────
        # These are human-readable sentences derived purely from the numbers above.
        # They are used verbatim by the Responder — no LLM generation involved.
        risk_factors = []
        if blocked:
            hrs = sum(t.get("estimated_time", 0) for t in blocked)
            risk_factors.append(
                f"{len(blocked)} task(s) blocked -- {hrs}h of work stalled"
            )
        if avg_var > 0.10:
            risk_factors.append(
                f"Tasks running {round(avg_var * 100)}% over original estimates on average"
            )
        if buf < 0:
            risk_factors.append(
                f"Deadline already missed -- {abs(buf)} days overdue"
            )
        elif buf < 14:
            risk_factors.append(
                f"Deadline buffer critically low -- only {buf} days remaining"
            )
        # Flag any individual team member whose load ratio is above 1.4
        overloaded = [m for m in team_analysis if m["load_status"] == "overloaded"]
        for mb in overloaded:
            risk_factors.append(
                f"{mb['name']} is overloaded ({mb['tasks_assigned']} tasks, "
                f"load ratio {mb['load_ratio']}x average)"
            )
        if not risk_factors:
            risk_factors.append("No major risk factors detected -- project is healthy")

        # ── 10. Priority recommendations ──────────────────────────────────────
        # A short ordered list of the most impactful actions the PM should take.
        # Generated from the data, not from an LLM.
        priorities = []
        if blocked:
            priorities.append(
                f"Unblock {len(blocked)} stalled task(s) -- "
                f"{sum(t['hours'] for t in blocked_details)}h at stake"
            )
        if overloaded:
            priorities.append(
                f"Redistribute tasks from {overloaded[0]['name']} "
                f"(load ratio: {overloaded[0]['load_ratio']}x)"
            )
        underutil = [m for m in team_analysis if m["load_status"] == "underutilised"]
        if underutil:
            priorities.append(
                f"Assign more work to {underutil[0]['name']} "
                f"({underutil[0]['capacity_per_week']}h/wk capacity underused)"
            )
        if avg_var > 0.15:
            priorities.append(
                "Review task estimates -- consistent overruns detected"
            )
        if not priorities:
            priorities.append("Maintain current pace and monitor velocity weekly")

        # ── 11. Release readiness (if present in raw data) ───────────────────
        # Release readiness data is populated from the Release Management
        # Platform JSON files (project_atlas.json, etc.). It is absent for
        # live Jira-synced projects, so we read it with .get() and default
        # every field to a safe sentinel so downstream code never KeyErrors.
        rr = data.get("release_readiness", {})
        readiness_color    = rr.get("readiness_color", "UNKNOWN")
        rr_blockers        = rr.get("blockers", [])
        rr_qa_gaps         = rr.get("qa_gaps", [])
        rr_doc_gaps        = rr.get("doc_gaps", [])
        rr_cross_deps      = rr.get("cross_dependencies", [])
        servicenow_cr      = rr.get("servicenow_cr", "")
        cab_status         = rr.get("cab_status", "")
        go_nogo            = rr.get("go_nogo_recommendation", "")

        # ── Override CAB status from live ServiceNow snapshot if available ────
        sn_snapshot = context.servicenow_data
        if sn_snapshot:
            proj_key = context.project_id.upper()
            for cr in sn_snapshot.get("change_requests", []):
                desc = (cr.get("short_description") or "").upper()
                if proj_key in desc:
                    if cr.get("number"):
                        servicenow_cr = cr["number"]
                    if cr.get("cab_decision"):
                        cab_status = cr["cab_decision"]
                    if cr.get("state"):
                        go_nogo = "GO" if "approved" in cr["cab_decision"].lower() else "NO-GO"
                    break

        # Historical context — team track record used for risk commentary
        hc = data.get("historical_context", {})
        hist_rollbacks     = hc.get("rollbacks_last_12_months", 0)
        hist_overruns      = hc.get("deployment_overruns", "")
        hist_known_issue   = hc.get("known_issue", "")
        hist_doc_avg       = hc.get("doc_completeness_avg", "N/A")
        hist_pass_rate     = hc.get("first_submission_pass_rate", "N/A")

        # Fold release readiness blockers into the main risk_factors list so they
        # appear in every risk/summary answer, not only the readiness-specific one.
        for b in rr_blockers[:3]:
            risk_factors.append(f"Release blocker: {b}")

        # ── 12. Confluence insights (if available) ────────────────────────────
        # Parse meeting notes and other Confluence pages to extract mentions of
        # blockers, decisions, action items, and risks.
        # These supplement the Jira/task data with qualitative context.
        confluence_blockers  = []
        confluence_risks     = []
        confluence_decisions = []
        confluence_actions   = []

        if context.has_confluence:
            pages = context.confluence_data.get("pages", [])
            for page in pages:
                content = page.get("content", "").lower()
                title   = page.get("title", "")
                date    = page.get("last_updated", "")

                # ── Extract sentences from meeting notes ───────────────────────
                # Meeting note pages (sprint reviews, standups, retros) are
                # scanned sentence by sentence for specific signal words.
                if page.get("is_meeting_note"):
                    for sentence in page.get("content", "").split("."):
                        s = sentence.strip()
                        # Blocker mentions: anything that sounds like something is stuck
                        if any(w in s.lower() for w in ["block", "stuck", "waiting", "impediment", "delay"]):
                            if s:
                                confluence_blockers.append(f"[{date} - {title}] {s[:120]}")
                        # Decision mentions: agreed-upon choices made in the meeting
                        if any(w in s.lower() for w in ["decided", "decision", "agreed", "approved"]):
                            if s:
                                confluence_decisions.append(f"[{date} - {title}] {s[:120]}")
                        # Action item mentions: follow-ups assigned during the meeting
                        if any(w in s.lower() for w in ["action", "todo", "follow up", "next step", "will "]):
                            if s:
                                confluence_actions.append(f"[{date} - {title}] {s[:120]}")

                # ── Extract risks from non-meeting pages ───────────────────────
                # Regular documentation pages (specs, runbooks) often mention
                # risks or concerns that don't appear in Jira tickets.
                if not page.get("is_meeting_note"):
                    for sentence in page.get("content", "").split("."):
                        s = sentence.strip()
                        if any(w in s.lower() for w in ["risk", "concern", "issue", "problem", "challenge"]):
                            if s:
                                confluence_risks.append(f"[{title}] {s[:120]}")

            # Confluence blockers are serious enough to add directly to risk_factors
            # so they appear in every risk summary, not just the Confluence section.
            for b in confluence_blockers[:3]:
                risk_factors.append(f"Confluence blocker: {b}")

        # ── Store metrics in context ───────────────────────────────────────────
        # All values are scalars, lists of scalars, or lists of dicts.
        # The Responder reads only from this dict — it never touches raw_data.
        context.metrics = {
            # Release readiness (Release Management Platform fields)
            "readiness_color":   readiness_color,
            "rr_blockers":       rr_blockers,
            "rr_qa_gaps":        rr_qa_gaps,
            "rr_doc_gaps":       rr_doc_gaps,
            "rr_cross_deps":     rr_cross_deps,
            "servicenow_cr":     servicenow_cr,
            "cab_status":        cab_status,
            "go_nogo":           go_nogo,
            # Historical context
            "hist_rollbacks":    hist_rollbacks,
            "hist_overruns":     hist_overruns,
            "hist_known_issue":  hist_known_issue,
            "hist_doc_avg":      hist_doc_avg,
            "hist_pass_rate":    hist_pass_rate,
            "project_id":               project.get("id", ""),
            "project_name":             project["name"],
            "description":              project.get("description", ""),
            "start_date":               project.get("start_date", "N/A"),
            "deadline":                 project["deadline"],
            "data_source":              context.data_source,
            # Task counts
            "tasks_total":              total,
            "tasks_done":               len(done),
            "tasks_in_progress":        len(in_prog),
            "tasks_blocked":            len(blocked),
            "tasks_todo":               len(todo),
            "completion_pct":           pct,
            # Hours
            "remaining_hours":          round(remaining, 1),
            "adjusted_remaining_hours": round(adjusted, 1),
            "time_variance_pct":        round(avg_var * 100, 1),
            # Timeline
            "daily_capacity":           round(daily_cap, 1),
            "estimated_days_remaining": round(days_left, 1),
            "projected_finish":         str(proj_fin),
            "buffer_days":              buf,
            "on_track":                 on_track,
            # Risk
            "risk_level":               risk,
            "risk_factors":             risk_factors,
            # Team
            "team_analysis":            team_analysis,
            # Details
            "blocked_tasks":            blocked_details,
            "priorities":               priorities,
            # Confluence
            "confluence_blockers":      confluence_blockers[:5],
            "confluence_risks":         confluence_risks[:5],
            "confluence_decisions":     confluence_decisions[:5],
            "confluence_actions":       confluence_actions[:5],
            "confluence_page_count":    len(context.confluence_data.get("pages", [])),
            "confluence_space":         context.confluence_data.get("space_name", ""),
            "confluence_page_titles":   [p.get("title","") for p in context.confluence_data.get("pages", [])],
        }

        return context
