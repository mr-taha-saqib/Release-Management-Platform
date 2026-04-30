"""
mcp/responder.py -- Layer 3: Responder

Responsibility:
  * Accept a natural-language question and a processed ContextStore.
  * Route the question to the correct answer handler using keyword tiers.
  * Build a structured plain-text answer exclusively from context.metrics.
  * Return the answer string.

Guarantee:
  Every value printed in an answer is taken directly from context.metrics,
  which was computed by the Processor from the raw data.
  The Responder never generates, guesses, or invents any value.

Routing tiers (in priority order):
  1. Blocker questions      -> _answer_blockers()
  2. Priority / actions     -> _answer_priorities()
  3. Resource / team        -> _answer_resources()
  4. Release readiness      -> _answer_release_readiness()
  5. Confluence docs        -> _answer_confluence()
  6. Summary / overview     -> _answer_summary()
  7. Risk / delay           -> _answer_risks()
  8. Timeline / progress    -> _answer_timeline()
  9. Fallback               -> _answer_summary()

Usage:
    ctx = ContextStore.load("flask")
    ctx = Processor().process(ctx)
    answer = Responder().answer("What are the blocked tasks?", ctx)
    print(answer)
"""

from __future__ import annotations
from mcp.context import ContextStore


def _try_ollama(question: str, context: ContextStore) -> str | None:
    """
    Attempt to answer the question using Ollama (local LLM).

    Ollama is the preferred answering method because it provides
    true natural-language reasoning rather than template-based responses.
    This function returns None silently if Ollama is not running,
    allowing the caller to fall back to the rule-based Responder.

    How it works:
      1. Check if Ollama is reachable on localhost:11434.
      2. Build a rich structured context string from context.metrics
         (the same computed data the rule-based handlers use).
      3. Pass that context + the user's question to _query_ollama().
      4. Suppress the streaming stdout output (Ollama prints tokens as
         they arrive; the caller handles display separately).
      5. Return the complete answer string.

    Parameters
    ----------
    question : str
        The user's natural-language question.
    context : ContextStore
        A fully processed ContextStore (context.is_processed must be True).

    Returns
    -------
    str
        The LLM's answer if Ollama is running and responded successfully.
    None
        If Ollama is not running, the model isn't loaded, or any error occurs.
    """
    try:
        from src.llm_interface import _ollama_is_running, _ollama_has_model, _query_ollama
        if not _ollama_is_running():
            return None  # Ollama server not running — fall through to rule-based
        if not _ollama_has_model():
            return None  # Model not pulled — fall through to rule-based

        m = context.metrics

        # ── Build team load summary for the LLM context ───────────────────────
        # Format each team member as a single line so the LLM can reason
        # about workload balance without needing to parse a nested structure.
        team_lines = "\n".join(
            f"  - {t['name']} ({t['role']}): "
            f"{t['tasks_assigned']} tasks, load={t['load_ratio']:.2f}x"
            + (" [OVERLOADED]" if t.get("overloaded") else "")
            + (" [UNDERUSED]"  if t.get("underused")  else "")
            for t in m.get("team_analysis", [])
        )

        # ── Build blocked task list for the LLM context ───────────────────────
        blocked_lines = "\n".join(
            f"  - [{b['id']}] {b['title']} (assigned: {b['assigned_to']}, {b['hours']}h)"
            for b in m.get("blocked_tasks", [])
        ) or "  None"

        # ── Build release readiness section for LLM context ──────────────────
        rr_color   = m.get("readiness_color", "")
        rr_go_nogo = m.get("go_nogo", "")
        rr_section = ""
        if rr_color:
            rr_blockers_lines = "\n".join(f"  - {b}" for b in m.get("rr_blockers", [])) or "  None"
            rr_section = f"""
RELEASE READINESS
Readiness colour : {rr_color}
Go/No-Go         : {rr_go_nogo}
ServiceNow CR    : {m.get('servicenow_cr', 'N/A')}  |  CAB: {m.get('cab_status', 'N/A')}
Release blockers :
{rr_blockers_lines}
Historical: {m.get('hist_rollbacks', 0)} rollbacks | overruns: {m.get('hist_overruns', 'N/A')}
"""

        # ── Build optional Confluence section ─────────────────────────────────
        # Only included when Confluence data was fetched and processed.
        # Gives the LLM access to meeting notes, decisions, and action items
        # that are not captured in Jira ticket data.
        conf_section = ""
        if m.get("confluence_space"):
            conf_blockers  = "\n".join(f"  - {b}" for b in m.get("confluence_blockers", [])) or "  None"
            conf_decisions = "\n".join(f"  - {d}" for d in m.get("confluence_decisions", [])) or "  None"
            conf_actions   = "\n".join(f"  - {a}" for a in m.get("confluence_actions", [])) or "  None"
            conf_risks     = "\n".join(f"  - {r}" for r in m.get("confluence_risks", [])) or "  None"
            conf_pages     = "\n".join(f"  - {p}" for p in m.get("confluence_page_titles", [])) or "  None"
            conf_section   = f"""
CONFLUENCE ({m['confluence_space']} -- {m['confluence_page_count']} pages)
Page titles:
{conf_pages}
Blockers reported in meeting notes:
{conf_blockers}
Key decisions:
{conf_decisions}
Action items:
{conf_actions}
Documented risks:
{conf_risks}
"""

        # ── Assemble the full context string passed to Ollama ─────────────────
        # This is the "system prompt" context — all computed metrics laid out
        # in a structured plain-text format that the LLM can parse and cite.
        try:
            all_projects  = ContextStore.list_all()
            project_list  = ", ".join(p["name"] for p in all_projects)
            total_projects = len(all_projects)
        except Exception:
            project_list  = m['project_name']
            total_projects = 1

        # ── Build full Jira task list ──────────────────────────────────────────
        all_tasks = context.tasks
        tasks_lines = "\n".join(
            f"  [{t.get('id','')}] {t.get('title','')[:80]} | status={t.get('status','')} | assignee={t.get('assigned_to','')} | {t.get('estimated_time',0)}h"
            for t in all_tasks
        ) or "  None"

        # ── Build full Confluence page list ────────────────────────────────────
        conf_pages_full = ""
        if context.has_confluence:
            pages = context.confluence_data.get("pages", [])
            conf_pages_full = "CONFLUENCE PAGES (full list):\n" + "\n".join(
                f"  [{p.get('last_updated','')[:10]}] {p.get('title','')} | {p.get('content','')[:300].strip()}"
                for p in pages
            )

        # ── Build ServiceNow section ────────────────────────────────────────────
        sn_section = ""
        sn = context.servicenow_data
        if sn:
            proj_key = context.project_id.upper()
            crs = [cr for cr in sn.get("change_requests", [])
                   if proj_key in (cr.get("short_description","") or "").upper()]
            tasks_sn = sn.get("change_tasks", [])
            cis  = sn.get("cmdb_cis", [])
            incs = sn.get("incidents", [])
            sn_section = f"""
SERVICENOW DATA:
Change Requests : {len(crs)}
{chr(10).join(f"  CR={cr.get('number','')} | {cr.get('short_description','')} | state={cr.get('state','')} | CAB={cr.get('cab_decision','')}" for cr in crs)}
Change Tasks    : {len(tasks_sn)}
{chr(10).join(f"  [{ct.get('number','')}] {ct.get('description','')} | state={ct.get('state','')}" for ct in tasks_sn[:10])}
CMDB CIs        : {", ".join(ci.get('name','') for ci in cis)}
Incidents       : {len(incs)}
"""

        rich_context = f"""PLATFORM: Release Management Platform
Total projects : {total_projects}
All projects   : {project_list}
Currently viewing: {m['project_name']}

PROJECT: {m['project_name']}
Data source  : {m['data_source']}
Deadline     : {m['deadline']}
Completion   : {m['completion_pct']}%  ({m['tasks_done']} done / {m['tasks_total']} total)
In Progress  : {m['tasks_in_progress']}
Blocked      : {m['tasks_blocked']}
To Do        : {m['tasks_todo']}

TIMELINE
Projected finish : {m['projected_finish']}
Buffer days      : {m['buffer_days']} days
On track         : {'YES' if m['on_track'] else 'NO - BEHIND SCHEDULE'}
Hours remaining  : {m['adjusted_remaining_hours']}h
Daily capacity   : {m['daily_capacity']}h/day

RISK
Risk level   : {m['risk_level']}
Risk factors :
{chr(10).join('  - ' + r for r in m.get('risk_factors', ['None']))}

ALL JIRA TASKS:
{tasks_lines}

TEAM
{team_lines or '  No team data'}

PRIORITIES
{chr(10).join('  ' + str(i+1) + '. ' + p for i, p in enumerate(m.get('priorities', [])))}
{rr_section}{conf_section}
{conf_pages_full}
{sn_section}"""

        # ── Call Ollama, suppressing its streaming stdout ──────────────────────
        # _query_ollama() streams tokens to stdout as they arrive (for the CLI).
        # In this context the caller (answer()) handles display, so we redirect
        # stdout to a buffer to avoid printing the same output twice.
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = _query_ollama(rich_context, question)
        return result

    except Exception:
        # Any error (network, model not loaded, JSON parse error, etc.)
        # is swallowed here so the pipeline always falls back gracefully.
        return None


# ── Keyword sets ───────────────────────────────────────────────────────────────
# Each set defines which user question words trigger a particular answer handler.
# Matching is substring-based (any keyword appears anywhere in the question).
# Sets are checked in priority order in Responder.answer().

_BLOCKER_KW  = {"block", "stuck", "stall", "impediment", "stopp",
                "unblock", "halt", "impedance"}

_RESOURCE_KW = {"resource", "workload", "overload", "underutil",
                "capacity", "who is assign", "who has", "team member",
                "staff", "who is over", "who is under", "load ratio",
                "reassign", "redistribute"}

_RISK_KW     = {"risk", "delay", "late", "behind", "overdue", "danger",
                "threat", "miss deadline", "concern", "worst case"}

_PRIORITY_KW = {"priorit", "next step", "what should", "recommend",
                "action item", "focus on", "most urgent", "what to do",
                "key action"}

_SUMMARY_KW  = {"summary", "overview", "overall", "brief",
                "describe the project", "tell me about", "general",
                "health check", "project report", "how is the project",
                "status of the project", "give me a report"}

_TIMELINE_KW = {"when will", "when does", "finish date", "completion date",
                "how long", "days remaining", "weeks remaining",
                "deadline", "how many days", "time to complete",
                "eta", "forecast", "completion estimate", "will it finish",
                "schedule", "buffer", "on track", "behind schedule"}

_TEAM_KW     = {"who", "team", "member", "assign", "person", "people",
                "developer", "engineer", "designer", "contributor", "role"}

_PROGRESS_KW = {"progress", "percent", "how far", "completion", "how much",
                "done so far", "how complete", "tasks done", "task count"}

_CONFLUENCE_KW = {"confluence", "meeting", "notes", "decision", "standup",
                  "documentation", "doc", "wiki", "retro", "retrospective",
                  "action item", "agreed", "planning notes"}

_RELEASE_KW = {"release", "readiness", "go-nogo", "go/no-go", "go nogo",
               "cab", "change advisory", "change request", "servicenow",
               "servicenow", "cr number", "deployment ready", "release ready",
               "can we release", "release status", "ship", "production ready",
               "qa gap", "doc gap", "rollback history", "historical"}


class Responder:
    """
    Stateless question router and answer builder (Layer 3).

    Call answer(question, context) to get a structured response.

    Answer priority:
      1. Ollama (local LLM) — if running, produces free-form natural language.
      2. Rule-based keyword handlers — if Ollama is unavailable, each handler
         formats a structured plain-text answer from context.metrics.
    """

    def answer(self, question: str, context: ContextStore) -> str:
        """
        Route the question to the appropriate handler and return a plain-text answer.

        The routing logic works in two stages:
          Stage 1: Try Ollama. If it answers, return that immediately.
          Stage 2: Match the question against keyword sets (Tiers 1-7).
                   The first matching tier wins. Tier 8 is a catch-all fallback.

        Parameters
        ----------
        question : str
            Natural-language question from the user (any length, any phrasing).
        context  : ContextStore
            Must already be processed (context.is_processed == True).
            The Processor populates context.metrics before this is called.

        Returns
        -------
        str
            A multi-line plain-text answer built entirely from context.metrics.

        Raises
        ------
        RuntimeError
            If context has not been processed yet (context.metrics is None).
        """
        if not context.is_processed:
            raise RuntimeError(
                "ContextStore must be processed before answering. "
                "Call Processor().process(context) first."
            )

        # ── Stage 1: Try Ollama (real local LLM) ──────────────────────────────
        # Ollama provides free-form natural language answers using the full
        # computed metrics as context. Falls back silently if not running.
        ollama_answer = _try_ollama(question, context)
        if ollama_answer:
            return ollama_answer

        # ── Stage 2: Rule-based keyword routing ───────────────────────────────
        # Lower the question for case-insensitive matching against keyword sets.
        m = context.metrics
        q = question.lower().strip()

        # Tier 1: Blocker questions — highest priority because blockers are urgent
        if self._match(q, _BLOCKER_KW):
            return self._answer_blockers(m)
        # Tier 2: Priority / action items — "what should I do next?"
        if self._match(q, _PRIORITY_KW):
            return self._answer_priorities(m)
        # Tier 3: Resource / team — workload balance, who is assigned what
        if self._match(q, _RESOURCE_KW) or self._match(q, _TEAM_KW):
            return self._answer_resources(m)
        # Tier 4: Release readiness — go/no-go, CAB status, ServiceNow CR
        if self._match(q, _RELEASE_KW):
            return self._answer_release_readiness(m)
        # Tier 5: Confluence — meeting notes, decisions, wiki docs
        if self._match(q, _CONFLUENCE_KW):
            return self._answer_confluence(m)
        # Tier 6: Summary / overview — general health check
        if self._match(q, _SUMMARY_KW):
            return self._answer_summary(m)
        # Tier 7: Risk / delay — deadline danger, overdue tasks
        if self._match(q, _RISK_KW):
            return self._answer_risks(m)
        # Tier 8: Timeline / progress — completion dates, days remaining
        if self._match(q, _TIMELINE_KW) or self._match(q, _PROGRESS_KW):
            return self._answer_timeline(m)

        # Tier 9: Fallback — question didn't match any keyword, show full summary
        return self._answer_summary(m)

    # ── Routing helper ─────────────────────────────────────────────────────────

    @staticmethod
    def _match(text: str, keywords: set) -> bool:
        """Return True if any keyword in the set appears anywhere in text."""
        return any(kw in text for kw in keywords)

    # ── Format helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _risk_label(level: str) -> str:
        """Convert a raw risk level string to a colour-annotated display label."""
        return {
            "LOW":      "LOW      (GREEN)",
            "MEDIUM":   "MEDIUM   (YELLOW)",
            "HIGH":     "HIGH     (ORANGE)",
            "CRITICAL": "CRITICAL (RED)",
        }.get(level, level)

    @staticmethod
    def _bar(pct: float, width: int = 20) -> str:
        """
        Render a simple ASCII progress bar.

        Example output: [========------------] 40.0%

        Parameters
        ----------
        pct   : float  Percentage to fill (clamped to 0-100).
        width : int    Total number of characters inside the brackets.
        """
        pct    = max(0.0, min(100.0, float(pct)))
        filled = round(pct / 100 * width)
        return "[" + "=" * filled + "-" * (width - filled) + f"] {pct}%"

    @staticmethod
    def _div(w: int = 50) -> str:
        """Return a horizontal divider line of width w."""
        return "-" * w

    # ── Answer handlers (all values pulled from metrics dict) ─────────────────
    # Each handler receives `m` (the metrics dict) and formats a multi-line
    # plain-text answer. No arithmetic is done here — all numbers come from
    # context.metrics, which the Processor already computed.

    def _answer_timeline(self, m: dict) -> str:
        """
        Answer questions about project completion date and schedule health.

        Shows: progress bar, on-track status, raw vs variance-adjusted hours,
        daily team capacity, key dates, and buffer days until deadline.
        """
        # Choose the buffer message based on whether we're ahead, on, or past deadline
        buf_msg = (
            f"This leaves a {m['buffer_days']}-day buffer before the deadline."
            if m["buffer_days"] > 0
            else (
                "The project is projected to finish exactly on the deadline."
                if m["buffer_days"] == 0
                else f"WARNING: Deadline already missed by {abs(m['buffer_days'])} days."
            )
        )
        lines = [
            f"COMPLETION ESTIMATE -- {m['project_name']}",
            self._div(),
            f"Data source    : {m['data_source']}",
            "",
            f"Progress       : {self._bar(m['completion_pct'])}",
            f"Status         : {'ON TRACK' if m['on_track'] else 'BEHIND SCHEDULE'}",
            "",
            "Work Remaining:",
            f"  Raw hours left       : {m['remaining_hours']}h",
            f"  Variance-adjusted    : {m['adjusted_remaining_hours']}h  "
            f"(+{m['time_variance_pct']}% from historical over-runs)",
            f"  Team daily capacity  : {m['daily_capacity']}h/day",
            f"  Days to finish       : ~{m['estimated_days_remaining']} working days",
            "",
            "Key Dates:",
            f"  Project start    : {m['start_date']}",
            f"  Projected finish : {m['projected_finish']}",
            f"  Deadline         : {m['deadline']}",
            f"  Buffer           : {m['buffer_days']} days",
            "",
            buf_msg,
        ]
        # Warn the user if historical variance is so high it significantly
        # inflates the adjusted estimate — they may want to re-estimate tasks.
        if m["time_variance_pct"] > 20:
            lines.append(
                f"\nNote: {m['time_variance_pct']}% historical over-run detected. "
                "Adjusted estimate already accounts for this."
            )
        return "\n".join(lines)

    def _answer_risks(self, m: dict) -> str:
        """
        Answer questions about project risk, delays, and danger signals.

        Shows: overall risk level, projected finish vs deadline, risk factors
        derived from data, and actionable guidance scaled to the risk level.
        """
        lines = [
            f"RISK ANALYSIS -- {m['project_name']}",
            self._div(),
            f"Data source        : {m['data_source']}",
            "",
            f"Overall Risk Level : {self._risk_label(m['risk_level'])}",
            f"Projected Finish   : {m['projected_finish']}",
            f"Deadline           : {m['deadline']}",
            f"Buffer             : {m['buffer_days']} days",
            "",
            "Risk Factors (derived from data):",
        ]
        # List all risk factor sentences generated by the Processor
        for i, f in enumerate(m["risk_factors"], 1):
            lines.append(f"  {i}. {f}")

        lines += [
            "",
            "Supporting Metrics:",
            f"  Blocked tasks   : {m['tasks_blocked']}",
            f"  Time variance   : {m['time_variance_pct']}%",
            f"  Deadline buffer : {m['buffer_days']} days",
        ]

        # Scale the recommended actions to the severity of the risk level
        level = m["risk_level"]
        if level == "CRITICAL":
            lines += [
                "",
                "ACTION REQUIRED:",
                "  1. Resolve all blocked tasks today.",
                "  2. Escalate to stakeholders.",
                "  3. Defer low-priority tasks to reduce scope.",
                "  4. Re-estimate remaining work.",
            ]
        elif level == "HIGH":
            lines += ["", "CAUTION: Manageable but requires immediate attention."]
        elif level == "MEDIUM":
            lines += ["", "WATCH: Monitor blockers and variance weekly."]
        else:
            lines += ["", "Project is in good health. Continue current pace."]

        # Append Confluence documented risks if available
        conf_risks = m.get("confluence_risks", [])
        if conf_risks:
            lines += ["", "Documented Risks (Confluence):"]
            for r in conf_risks:
                lines.append(f"  - {r}")

        return "\n".join(lines)

    def _answer_blockers(self, m: dict) -> str:
        """
        Answer questions about blocked tasks.

        Shows: number of blocked tasks, total hours at stake, per-task details
        (ID, priority, assignee, hours, data origin), and recommended actions.
        Also appends Confluence-reported blockers from meeting notes if available.
        """
        blocked = m["blocked_tasks"]
        lines   = [
            f"BLOCKED TASKS -- {m['project_name']}",
            self._div(),
            f"Data source : {m['data_source']}",
            "",
        ]
        if not blocked:
            lines += [
                "No blocked tasks. All work is progressing normally.",
                "",
                "Note: Task status is read directly from the project data file.",
            ]
            return "\n".join(lines)

        total_h = sum(t["hours"] for t in blocked)
        lines.append(
            f"{len(blocked)} task(s) BLOCKED -- {total_h}h of work stalled.\n"
        )
        # Show details for each blocked task
        for i, t in enumerate(blocked, 1):
            # Label tasks as real API records vs synthetic supplemental records
            origin = "real GitHub issue" if t.get("real") else "supplemental record"
            lines += [
                f"  [{i}] {t['id']} -- {t['title'][:60]}",
                f"       Priority     : {t['priority']}",
                f"       Assigned to  : {t['assigned_to']}",
                f"       Hours at risk: {t['hours']}h",
                f"       Record type  : {origin}",
                "",
            ]
        lines += [
            "Recommended Actions:",
            "  1. Hold emergency stand-up to identify root cause.",
            "  2. Assign single owner per blocked task.",
            "  3. Set 24-hour resolution target.",
            "  4. Escalate to leadership if blockers exceed 48h.",
        ]

        # Append Confluence-reported blockers from meeting notes (if any)
        conf_blockers = m.get("confluence_blockers", [])
        if conf_blockers:
            lines += ["", "Blockers Reported in Meeting Notes (Confluence):"]
            for b in conf_blockers:
                lines.append(f"  - {b}")

        return "\n".join(lines)

    def _answer_resources(self, m: dict) -> str:
        """
        Answer questions about team workload, capacity, and assignment balance.

        Shows: team size, total capacity, per-member task completion and
        load ratio bar, overloaded/underutilised flags, and rebalancing advice.
        """
        team       = m["team_analysis"]
        overloaded = [x for x in team if x["load_status"] == "overloaded"]
        underutil  = [x for x in team if x["load_status"] == "underutilised"]

        lines = [
            f"RESOURCE ANALYSIS -- {m['project_name']}",
            self._div(),
            f"Data source    : {m['data_source']}",
            "",
            f"Team size      : {len(team)} members",
            f"Team capacity  : {m['daily_capacity']}h/day  "
            f"({round(m['daily_capacity'] * 5, 1)}h/week)",
            f"Tasks total    : {m['tasks_total']}   |   Completed: {m['tasks_done']}",
            "",
            "Individual Workload:",
            "",
        ]

        # Render one row per team member with a mini progress bar and load tag
        for mb in team:
            assigned = mb["tasks_assigned"]
            done     = mb["tasks_done"]
            pct      = round(done / assigned * 100) if assigned else 0
            bar      = self._bar(pct, width=12)  # narrower bar for per-member rows
            tag      = {"overloaded": " << OVERLOADED",
                        "underutilised": " << UNDERUSED",
                        "balanced": ""}.get(mb["load_status"], "")
            blk_note = f"  [{mb['tasks_blocked']} blocked]" if mb["tasks_blocked"] else ""
            lines.append(
                f"  {mb['name']:<12}  {mb['role']:<20}"
                f"  load={mb['load_ratio']}x  {done}/{assigned} tasks  "
                f"{bar}{blk_note}{tag}"
            )

        lines.append("")
        # Summarise who needs immediate attention
        if overloaded:
            names = ", ".join(x["name"] for x in overloaded)
            lines += [
                f"OVERLOADED : {names}",
                "  -> Redistribute tasks to prevent burnout.",
            ]
        if underutil:
            names = ", ".join(x["name"] for x in underutil)
            lines += [
                f"UNDERUSED  : {names}",
                "  -> Capacity available; assign more work.",
            ]
        if not overloaded and not underutil:
            lines.append("Team workload is BALANCED.")

        return "\n".join(lines)

    def _answer_priorities(self, m: dict) -> str:
        """
        Answer questions about what the PM should do next.

        Shows: risk level, deadline buffer, blocked task count, time variance,
        priority actions generated by the Processor, standing best practices,
        and escalation guidance for HIGH/CRITICAL risk levels.
        """
        lines = [
            f"RECOMMENDED ACTIONS -- {m['project_name']}",
            self._div(),
            f"Data source    : {m['data_source']}",
            "",
            f"Risk Level     : {self._risk_label(m['risk_level'])}",
            f"Deadline Buffer: {m['buffer_days']} days",
            f"Blocked Tasks  : {m['tasks_blocked']}",
            f"Time Variance  : {m['time_variance_pct']}%",
            "",
            "Priority Actions (derived from current data):",
        ]
        # These priorities were computed by the Processor from actual metrics
        for i, p in enumerate(m["priorities"], 1):
            lines.append(f"  {i}. {p}")

        lines += [
            "",
            "Standing Recommendations:",
            "  * Daily stand-up to surface blockers early.",
            "  * Re-estimate tasks with >30% variance.",
            "  * Review resource allocation if load ratio >1.3x.",
        ]
        # Add urgency note for high-risk projects
        if m["risk_level"] in ("CRITICAL", "HIGH"):
            lines += [
                "",
                f"ESCALATION: Risk level is {m['risk_level']}.",
                "  Communicate status to stakeholders this week.",
            ]

        # Append Confluence action items from meeting notes if available
        conf_actions = m.get("confluence_actions", [])
        if conf_actions:
            lines += ["", "Action Items from Meeting Notes (Confluence):"]
            for a in conf_actions:
                lines.append(f"  - {a}")

        return "\n".join(lines)

    def _answer_release_readiness(self, m: dict) -> str:
        """
        Answer questions about release readiness, go/no-go status, CAB approval,
        and historical team track record.

        Shows: readiness colour, go/no-go recommendation, ServiceNow CR and CAB
        status, any open blockers, QA gaps, documentation gaps, cross-project
        dependencies, and the team's historical delivery track record.
        """
        color = m.get("readiness_color", "UNKNOWN")
        go_nogo = m.get("go_nogo", "Not assessed")
        cr = m.get("servicenow_cr", "")
        cab = m.get("cab_status", "")

        # Colour label with short explanation for each state
        color_labels = {
            "GREEN":   "GREEN  — Ready for release",
            "AMBER":   "AMBER  — Conditional / issues present",
            "RED":     "RED    — NOT ready — blockers open",
            "UNKNOWN": "UNKNOWN — No readiness data available",
        }
        color_label = color_labels.get(color, color)

        lines = [
            f"RELEASE READINESS -- {m['project_name']}",
            self._div(),
            f"Data source      : {m['data_source']}",
            "",
            f"Readiness Status : {color_label}",
            f"Go/No-Go         : {go_nogo}",
            "",
            "ServiceNow Change Request:",
            f"  CR Number  : {cr or 'Not assigned'}",
            f"  CAB Status : {cab or 'Not submitted'}",
            "",
        ]

        # ── Open blockers ──────────────────────────────────────────────────────
        blockers = m.get("rr_blockers", [])
        if blockers:
            lines.append("Open Blockers:")
            for b in blockers:
                lines.append(f"  * {b}")
            lines.append("")
        else:
            lines += ["Open Blockers:", "  None — all blockers resolved", ""]

        # ── QA gaps ───────────────────────────────────────────────────────────
        qa_gaps = m.get("rr_qa_gaps", [])
        if qa_gaps:
            lines.append("QA Gaps (missing test coverage):")
            for g in qa_gaps:
                lines.append(f"  ! {g}")
            lines.append("")

        # ── Documentation gaps ────────────────────────────────────────────────
        doc_gaps = m.get("rr_doc_gaps", [])
        if doc_gaps:
            lines.append("Documentation Gaps:")
            for g in doc_gaps:
                lines.append(f"  ! {g}")
            lines.append("")

        # ── Cross-project dependencies ─────────────────────────────────────────
        cross_deps = m.get("rr_cross_deps", [])
        if cross_deps:
            lines.append("Cross-Project Dependencies:")
            for d in cross_deps:
                lines.append(f"  -> {d}")
            lines.append("")

        # ── Historical team track record ───────────────────────────────────────
        lines += [
            "Historical Track Record:",
            f"  Rollbacks (12 months)        : {m.get('hist_rollbacks', 'N/A')}",
            f"  Deployment overruns          : {m.get('hist_overruns', 'N/A')}",
            f"  Doc completeness (avg)       : {m.get('hist_doc_avg', 'N/A')}",
            f"  CAB first-submission pass rate: {m.get('hist_pass_rate', 'N/A')}",
        ]
        known_issue = m.get("hist_known_issue", "")
        if known_issue and known_issue.lower() not in ("none", "none — team beta has the strongest track record",
                                                        "none — team delta has a clean delivery record"):
            lines += ["", f"  Known issue: {known_issue}"]

        # ── Actionable guidance based on colour ────────────────────────────────
        lines.append("")
        if color == "RED":
            lines += [
                "IMMEDIATE ACTIONS REQUIRED:",
                "  1. Resolve all open blockers before any further release steps.",
                "  2. Submit ServiceNow CR and schedule CAB review.",
                "  3. Fill all documentation gaps (runbook, test evidence, rollback).",
                "  4. Do not proceed to production until status moves to AMBER or GREEN.",
            ]
        elif color == "AMBER":
            lines += [
                "CONDITIONAL ACTIONS:",
                "  1. Resolve remaining blockers (if any) and re-test.",
                "  2. Confirm CAB approval is in progress.",
                "  3. Close any outstanding QA or documentation gaps.",
                "  4. Proceed only after all amber items are cleared.",
            ]
        else:
            lines.append("Project is cleared for release — proceed with deployment plan.")

        return "\n".join(lines)

    def _answer_confluence(self, m: dict) -> str:
        """
        Answer questions specifically about Confluence documentation.

        Shows: space name, page count, and extracted decisions, action items,
        blockers, and risks from meeting notes and documentation pages.
        If Confluence is not configured, explains how to set it up.
        """
        space = m.get("confluence_space", "")
        count = m.get("confluence_page_count", 0)

        # Confluence is optional — guide the user if it hasn't been configured
        if not space:
            return (
                "CONFLUENCE DOCS\n"
                + self._div() + "\n"
                "No Confluence data available for this project.\n\n"
                "To enable: set CONFLUENCE_SPACE_KEY in .env and run:\n"
                "  python3 connectors/sync.py"
            )

        lines = [
            f"CONFLUENCE DOCS -- {m['project_name']}",
            self._div(),
            f"Space      : {space}",
            f"Pages      : {count}",
            "",
        ]

        # Each section is shown only if the Processor extracted content for it
        decisions = m.get("confluence_decisions", [])
        if decisions:
            lines += ["Key Decisions:"]
            for d in decisions:
                lines.append(f"  - {d}")
            lines.append("")

        actions = m.get("confluence_actions", [])
        if actions:
            lines += ["Action Items:"]
            for a in actions:
                lines.append(f"  - {a}")
            lines.append("")

        blockers = m.get("confluence_blockers", [])
        if blockers:
            lines += ["Blockers Reported in Meeting Notes:"]
            for b in blockers:
                lines.append(f"  - {b}")
            lines.append("")

        risks = m.get("confluence_risks", [])
        if risks:
            lines += ["Documented Risks:"]
            for r in risks:
                lines.append(f"  - {r}")

        # If pages exist but no keywords matched, prompt user to add labels
        if not any([decisions, actions, blockers, risks]):
            lines.append("Pages found but no meeting notes, decisions or blockers extracted.")
            lines.append("Try adding labels like 'meeting' or 'sprint' to Confluence pages.")

        return "\n".join(lines)

    def _answer_summary(self, m: dict) -> str:
        """
        Answer general overview / health-check questions.

        This is also the fallback handler (Tier 8) for questions that don't
        match any specific keyword set. It shows everything at once:
        progress, status, task breakdown, timeline, team snapshot,
        risk factors, priorities, and a Confluence excerpt.
        """
        lines = [
            f"PROJECT OVERVIEW -- {m['project_name']}",
            self._div(),
            f"Data source : {m['data_source']}",
        ]
        # Optional project description if one was set in the JSON/Jira data
        if m.get("description"):
            lines += ["", f"  {m['description']}"]

        lines += [
            "",
            f"Progress : {self._bar(m['completion_pct'])}",
            f"Status   : {'On Track' if m['on_track'] else 'BEHIND SCHEDULE'}"
            f"   |   Risk: {self._risk_label(m['risk_level'])}",
            "",
            "Task Breakdown:",
            f"  Done        : {m['tasks_done']}",
            f"  In Progress : {m['tasks_in_progress']}",
            f"  Blocked     : {m['tasks_blocked']}",
            f"  To Do       : {m['tasks_todo']}",
            f"  Total       : {m['tasks_total']}",
            "",
            "Timeline:",
            f"  Start date       : {m['start_date']}",
            f"  Projected finish : {m['projected_finish']}"
            f"  (~{m['estimated_days_remaining']} working days from today)",
            f"  Deadline         : {m['deadline']}",
            f"  Buffer           : {m['buffer_days']} days",
            f"  Work remaining   : {m['adjusted_remaining_hours']}h"
            f"  (+{m['time_variance_pct']}% variance adjustment)",
            f"  Team capacity    : {m['daily_capacity']}h/day",
            "",
            "Team Snapshot:",
        ]
        # One line per team member: name, role, load ratio, task completion
        for mb in m["team_analysis"]:
            tag = {"overloaded": " <<OVERLOADED",
                   "underutilised": " <<UNDERUSED",
                   "balanced": ""}.get(mb["load_status"], "")
            lines.append(
                f"  {mb['name']:<12} {mb['role']:<20} "
                f"load={mb['load_ratio']}x  {mb['tasks_done']}/{mb['tasks_assigned']} done"
                f"{tag}"
            )

        lines += ["", "Key Risk Factors:"]
        for f in m["risk_factors"]:
            lines.append(f"  * {f}")

        lines += ["", "Top Priorities:"]
        for p in m["priorities"]:
            lines.append(f"  -> {p}")

        # Append a brief Confluence excerpt (decisions + blockers + actions)
        # only if Confluence data was fetched and processed
        if m.get("confluence_space"):
            lines += [
                "",
                f"Confluence ({m['confluence_space']} -- {m['confluence_page_count']} pages):",
            ]
            for d in m.get("confluence_decisions", [])[:2]:
                lines.append(f"  Decision  : {d}")
            for b in m.get("confluence_blockers", [])[:2]:
                lines.append(f"  Blocker   : {b}")
            for a in m.get("confluence_actions", [])[:2]:
                lines.append(f"  Action    : {a}")

        return "\n".join(lines)
