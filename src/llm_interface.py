"""
llm_interface.py - Route questions to a local LLM or the rule-based simulator.

Backend priority (first available wins):
  1. Ollama       - auto-detected if running on localhost:11434
  2. llama.cpp    - set LLAMA_CPP_ENABLED = True + provide a GGUF model file
  3. Claude API   - set ANTHROPIC_API_KEY env var (requires internet + paid key)
  4. Simulator    - rule-based, always available as fallback

Ollama setup (recommended for local use):
  1. Download and install Ollama from ollama.com
  2. Pull a model:
       ollama pull llama3.2      # 3B model, ~2 GB, fast on CPU
       ollama pull mistral       # 7B model, ~4 GB, better quality
       ollama pull phi4-mini     # 3.8B, good quality/speed balance
  3. Change OLLAMA_MODEL below to match the model you pulled.

llama.cpp setup (alternative):
  1. pip install llama-cpp-python
  2. Download a GGUF model file from HuggingFace
  3. Set LLAMA_CPP_ENABLED = True and update MODEL_PATH below
"""

import os
import re
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root so OLLAMA_MODEL and OLLAMA_URL are available
load_dotenv(Path(__file__).parent.parent / ".env")

# ── Configuration (reads from .env, falls back to defaults) ───────────────────
# Ollama settings — read from .env so the user can change them without editing code
OLLAMA_MODEL      = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_URL        = os.getenv("OLLAMA_URL",   "http://localhost:11434")

# llama.cpp settings — disabled by default; requires manual installation
LLAMA_CPP_ENABLED = False
MODEL_PATH        = Path("models/mistral-7b-instruct-v0.2.Q4_K_M.gguf")
N_CTX             = 4096   # context window size in tokens
N_THREADS         = 4      # CPU threads to use for inference

# Claude API settings — only used if ANTHROPIC_API_KEY env var is set
CLAUDE_MODEL      = "claude-opus-4-6"
# ──────────────────────────────────────────────────────────────────────────────

# ── Example questions (used by main.py menu and --demo mode) ──────────────────
EXAMPLE_QUESTIONS = [
    "When will the project be completed, and is the deadline at risk?",
    "Is the team workload balanced, and who is overloaded?",
    "What are the top 3 risks to address this sprint?",
    "Who is the biggest bottleneck and how do we unblock them?",
    "What are the most important actions for the PM this week?",
]
# ──────────────────────────────────────────────────────────────────────────────

# Lazy-loaded llama.cpp model instance — created on first use, reused after that
_llm_instance = None


# ── Ollama backend ─────────────────────────────────────────────────────────────

def _ollama_is_running() -> bool:
    """
    Check if the Ollama server is reachable on the configured URL.

    Makes a lightweight GET request to /api/tags (the model list endpoint).
    Returns False on any network error so callers can fall back gracefully.
    Timeout is 2 seconds to avoid blocking the UI on slow networks.
    """
    import requests
    try:
        return requests.get(f"{OLLAMA_URL}/api/tags", timeout=2).status_code == 200
    except Exception:
        return False


def _ollama_has_model() -> bool:
    """
    Check if the configured OLLAMA_MODEL has been pulled (downloaded) locally.

    Ollama stores model names with version tags (e.g. "llama3.2:latest").
    We compare only the base name (before the colon) for a more forgiving match.

    Returns False if the server is unreachable or the model list can't be parsed.
    """
    import requests
    try:
        r      = requests.get(f"{OLLAMA_URL}/api/tags", timeout=2)
        # Extract base names, stripping the ":latest" or ":3b" version suffix
        models = [m["name"].split(":")[0] for m in r.json().get("models", [])]
        return OLLAMA_MODEL.split(":")[0] in models
    except Exception:
        return False


def _query_ollama(context: str, question: str) -> str:
    """
    Send a question to Ollama and return the complete answer string.

    Uses the streaming chat API (/api/chat) so tokens are printed to stdout
    as they arrive — this gives the user live feedback for long responses.
    All tokens are also accumulated in a list and joined into the return value.

    The context string becomes part of the system prompt, giving the model
    all project metrics before it sees the user's question.

    Parameters
    ----------
    context  : str  Structured project metrics (built by the Responder).
    question : str  The user's natural-language question.

    Returns
    -------
    str  The complete model response (all streamed tokens joined together).
    """
    import requests, json

    # The system prompt instructs the model how to behave and injects project data
    system_prompt = (
        "You are a project management AI assistant. "
        "Answer ONLY using the project data provided below. "
        "Do NOT make up, guess, or invent any information not present in the data. "
        "If the data does not contain the answer, say 'I don't have that information in the project data.' "
        "Be concise and direct. For simple questions give short answers.\n\n"
        f"PROJECT DATA:\n{context}"
    )
    payload = {
        "model":    OLLAMA_MODEL,
        "stream":   True,   # stream tokens so the user sees output as it's generated
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": question},
        ],
    }
    collected = []
    # Open a streaming HTTP connection; each line is one JSON object with one token
    with requests.post(f"{OLLAMA_URL}/api/chat", json=payload, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if not raw:
                continue  # skip keep-alive blank lines
            chunk = json.loads(raw)
            token = chunk.get("message", {}).get("content", "")
            if token:
                print(token, end="", flush=True)   # live streaming output
                collected.append(token)
            if chunk.get("done"):
                break   # Ollama signals end of response
    print()  # final newline after the streamed answer
    return "".join(collected)


# ── Claude API backend ─────────────────────────────────────────────────────────

def _query_claude(context: str, question: str) -> str:
    """
    Send a question to the Anthropic Claude API and return the answer.

    Uses streaming so tokens appear in real time. Requires ANTHROPIC_API_KEY
    to be set in the environment. This is an internet-based, paid backend.

    Parameters
    ----------
    context  : str  Project metrics injected into the system prompt.
    question : str  The user's question.

    Returns
    -------
    str  Complete response text (all tokens joined).
    """
    import anthropic

    client = anthropic.Anthropic()
    system_prompt = (
        "You are an expert project management AI assistant. "
        "Analyse the project data below and answer with clear, concise, actionable insights.\n\n"
        f"{context}"
    )
    collected = []
    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        thinking={"type": "adaptive"},
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    ) as stream:
        for text in stream.text_stream:
            print(text, end="", flush=True)
            collected.append(text)
    print()
    return "".join(collected)


# ── llama.cpp backend ──────────────────────────────────────────────────────────

def _load_model():
    """
    Load the GGUF model file into memory using llama.cpp (lazy singleton).

    The model is expensive to load (~1-5 seconds), so it is created once
    on the first call and reused for all subsequent questions in the session.

    Returns
    -------
    Llama  The loaded llama.cpp model instance.

    Raises
    ------
    FileNotFoundError  If MODEL_PATH does not exist.
    ImportError        If llama-cpp-python is not installed.
    """
    global _llm_instance
    if _llm_instance is None:
        from llama_cpp import Llama
        if not MODEL_PATH.exists():
            raise FileNotFoundError(
                f"Model file not found: {MODEL_PATH}\n"
                "Download a GGUF model and set MODEL_PATH in src/llm_interface.py"
            )
        print(f"[LLM] Loading model: {MODEL_PATH} ...")
        _llm_instance = Llama(model_path=str(MODEL_PATH), n_ctx=N_CTX, n_threads=N_THREADS, verbose=False)
        print("[LLM] Model ready.")
    return _llm_instance


def _query_llama(context: str, question: str) -> str:
    """
    Ask a question using a locally loaded GGUF model via llama.cpp.

    Formats the prompt in the Mistral instruct template ([INST]...[/INST])
    which works for most instruction-tuned models. For non-Mistral models
    you may need to adjust the prompt format.

    Parameters
    ----------
    context  : str  Structured project metrics.
    question : str  The user's question.

    Returns
    -------
    str  The model's answer text.
    """
    model  = _load_model()
    # Mistral instruct format: wrap the full prompt in [INST]...[/INST] tags
    prompt = (
        "<s>[INST] You are an expert project management AI assistant. "
        "Analyse the project data below and answer clearly.\n\n"
        f"{context}\n\nQuestion: {question} [/INST]"
    )
    output = model(prompt, max_tokens=600, stop=["</s>", "[INST]"], echo=False)
    return output["choices"][0]["text"].strip()


# ── Context parsers (shared by simulator answer handlers) ─────────────────────

def _extract(context: str, key: str) -> str:
    """
    Return the value on the first line that contains `key:`.

    Used by simulator handlers to pull individual metric values out of
    the structured context string without parsing the whole document.

    Example:
        _extract("On Track: YES", "On Track")  ->  "YES"
    """
    for line in context.splitlines():
        if key.lower() in line.lower() and ":" in line:
            return line.split(":", 1)[-1].strip()
    return "N/A"


def _extract_section(context: str, header: str) -> list:
    """
    Return non-empty lines inside a named === section of the context string.

    Sections are delimited by lines like "=== SECTION NAME ===".
    Capture starts after the header line and stops at the next === line.

    Example:
        _extract_section(ctx, "IDENTIFIED RISKS")
        ->  ["Risk 1: ...", "Risk 2: ..."]
    """
    lines, capture = [], False
    for line in context.splitlines():
        if header.upper() in line.upper() and "===" in line:
            capture = True
            continue
        if capture:
            if line.startswith("==="):
                break  # hit the next section header — stop capturing
            if line.strip():
                lines.append(line.strip())
    return lines


def _parse_velocity(context: str) -> dict:
    """
    Extract sprint velocity history from the context string and compute trend metrics.

    Velocity history is stored as a space-separated list of integers
    (e.g. "32 28 25 22"). This function extracts those numbers and computes:
      - avg        : mean velocity across all sprints
      - latest     : most recent sprint's velocity
      - first      : first sprint's velocity (baseline)
      - trend_pct  : percentage change from first to last sprint
      - projected  : estimated next sprint velocity based on the last two sprints

    Returns
    -------
    dict  Keys: history, avg, latest, first, trend_pct, projected.
          All values are 0 / empty list if no velocity data is found.
    """
    hist_str = _extract(context, "Velocity History")
    nums     = [int(x) for x in re.findall(r'\d+', hist_str.split("(")[0])]
    if not nums:
        return {"history": [], "avg": 0, "latest": 0, "first": 0, "trend_pct": 0, "projected": 0}
    avg        = round(sum(nums) / len(nums), 1)
    trend_pct  = round((nums[-1] - nums[0]) / nums[0] * 100, 1) if len(nums) >= 2 else 0
    # Projected next sprint: apply the same sprint-to-sprint ratio as the last two sprints.
    # Floored at 5 to prevent unrealistically low projections.
    if len(nums) >= 2 and nums[-2] > 0:
        projected = max(5, round(nums[-1] * (nums[-1] / nums[-2])))
    else:
        projected = round(avg)
    return {"history": nums, "avg": avg, "latest": nums[-1], "first": nums[0],
            "trend_pct": trend_pct, "projected": projected}


def _parse_allocation(context: str) -> list:
    """
    Parse the RESOURCE ALLOCATION section of the context string into structured dicts.

    Each row in the section is expected to match a regex with named groups:
      name, role, total tasks, done, in_progress, blocked, capacity.

    Returns
    -------
    list of dict
        One dict per team member with keys:
        name, role, total, done, in_progress, blocked, capacity.
        Empty list if the section is missing or no rows match the pattern.
    """
    rows, people = _extract_section(context, "RESOURCE ALLOCATION"), []
    for row in rows:
        m = re.match(
            r'(.+?)\s*\((.+?)\).*?Total:\s*(\d+).*?Done:\s*(\d+)'
            r'.*?In Progress:\s*(\d+).*?Blocked:\s*(\d+).*?Capacity:\s*(\d+)',
            row,
        )
        if m:
            people.append({
                "name":        m.group(1).strip(),
                "role":        m.group(2).strip(),
                "total":       int(m.group(3)),
                "done":        int(m.group(4)),
                "in_progress": int(m.group(5)),
                "blocked":     int(m.group(6)),
                "capacity":    int(m.group(7)),
            })
    return people


def _days_between(d1: str, d2: str) -> int:
    """
    Calculate the number of days from date d1 to date d2 (both YYYY-MM-DD).

    Returns 0 if either date is missing or cannot be parsed.
    """
    try:
        return (datetime.strptime(d2, "%Y-%m-%d") - datetime.strptime(d1, "%Y-%m-%d")).days
    except Exception:
        return 0


# ── Simulator answer handlers ──────────────────────────────────────────────────
# These handlers produce structured, data-driven answers from the context string.
# They are used when no real LLM backend (Ollama, llama.cpp, Claude) is available.
# Each handler parses the relevant fields from the context and formats them
# into a multi-line plain-text report with a verdict and recommendations.

def _answer_timeline(context: str) -> str:
    """
    Simulator handler: answer timeline and completion date questions.

    Extracts: estimated end date, target date, on-track flag, remaining work,
    sprints needed, blocker count, and velocity history.
    Computes a worst-case scenario and formats a full TIMELINE FORECAST report.
    """
    est     = _extract(context, "Est. End Date")
    target  = _extract(context, "Target Date")
    track   = _extract(context, "On Track")
    remain  = _extract(context, "Remaining Total")
    sprints = _extract(context, "Sprints Needed")
    blocked = _extract(context, "Blocked")
    vel     = _parse_velocity(context)

    buffer_days = _days_between(est, target) if "N/A" not in (est, target) else "?"
    verdict     = f"ON TRACK  ({buffer_days}-day buffer)" if "YES" in track.upper() else "AT RISK"

    # Worst-case scenario: how many sprints if velocity continues to decline
    remain_pts = int(re.search(r'\d+', remain).group()) if re.search(r'\d+', remain) else 0
    if vel["projected"] > 0 and remain_pts:
        worst_sprints = round(remain_pts / vel["projected"], 1)
        worst_days    = round((worst_sprints - float(sprints.strip("~"))) * 14) if sprints != "N/A" else "?"
    else:
        worst_sprints, worst_days = "?", "?"

    lines = [
        "TIMELINE FORECAST",
        "-" * 17,
        f"  Verdict          : {verdict}",
        f"  Est. completion  : {est}",
        f"  Target deadline  : {target}",
        f"  Buffer           : {buffer_days} days",
        f"  Remaining work   : {remain}  (~{sprints} sprints at current avg)",
        "",
        f"Velocity Analysis: {vel['history']}",
        f"  Average            : {vel['avg']} pts/sprint",
        f"  Last sprint        : {vel['latest']} pts  ({vel['trend_pct']:+.0f}% vs Sprint 1)",
        f"  Trend              : {'DECLINING' if vel['trend_pct'] < -10 else 'STABLE'}",
        f"  Projected next     : ~{vel['projected']} pts  (if trend holds)",
        f"  Worst-case sprints : ~{worst_sprints}  (+{worst_days} days vs current estimate)",
        "",
        f"Blocker Impact: {blocked} stalled (PROJ-020, PROJ-021) -- not in above forecast.",
        f"  Resolving the vendor block recovers these pts but requires external action.",
        "",
        "Recommendations:",
        "  1. Watch Sprint 5 velocity -- it is the earliest leading indicator of deadline risk.",
        "  2. Resolve DataBridge vendor block before 2026-04-01 or start scope-reduction planning.",
        f"  3. Protect the {buffer_days}-day buffer -- communicate it to stakeholders as a health signal.",
    ]
    return "\n".join(lines)


def _answer_resources(context: str) -> str:
    """
    Simulator handler: answer resource allocation and workload balance questions.

    Parses all team members from the RESOURCE ALLOCATION section,
    calculates who is overloaded (>40% above avg) or underutilised (<40% below avg),
    and formats a detailed per-person breakdown with rebalancing recommendations.
    """
    people = _parse_allocation(context)
    if not people:
        return "No allocation data found in context."

    avg_total  = round(sum(p["total"] for p in people) / len(people), 1)
    overloaded = []
    underutil  = []

    lines = [
        "RESOURCE ALLOCATION",
        "-" * 19,
        f"  Team average : {avg_total} pts/person",
        "",
        f"  {'Name':<20} {'Role':<22} {'Total':>5}  Status",
        f"  {'-'*20} {'-'*22} {'-'*5}  {'-'*32}",
    ]

    # Sort by total descending so the most loaded person appears first
    for p in sorted(people, key=lambda x: x["total"], reverse=True):
        pct = round((p["total"] - avg_total) / avg_total * 100) if avg_total else 0
        if pct > 40:
            flag = f"*** OVERLOADED   ({pct:+d}% above avg)"
            overloaded.append(p)
        elif pct < -40:
            flag = f"--- UNDERUTILISED ({pct:+d}% below avg)"
            underutil.append(p)
        else:
            extra = ""
            if p["blocked"] > 0:
                extra = f"  [{p['blocked']} pts blocked]"
            flag = f"OK{extra}"
        lines.append(f"  {p['name']:<20} {p['role']:<22} {p['total']:>5}  {flag}")

    lines.append("")

    if overloaded:
        o = overloaded[0]
        lines += [
            "Key Issue -- Overload:",
            f"  {o['name']} owns {o['total']} pts: {o['done']} done, "
            f"{o['in_progress']} in progress, {o['blocked']} blocked.",
            f"  Blocked work creates a cascade -- downstream tasks wait on one person.",
        ]

    if underutil:
        u = underutil[0]
        spare = round(avg_total - u["total"])
        lines += [
            "",
            "Key Issue -- Underutilisation:",
            f"  {u['name']} has {u['capacity']} h/week capacity but only {u['total']} pts assigned.",
            f"  Could absorb ~{spare} pts of redistributed work without exceeding average.",
        ]

    lines += ["", "Recommendations:"]
    if overloaded and underutil:
        o, u = overloaded[0], underutil[0]
        lines += [
            f"  1. Move at least one To-Do ticket from {o['name'].split()[0]} to {u['name'].split()[0]}.",
            f"  2. Do not assign {o['name'].split()[0]} new tickets while {o['blocked']} pts remain blocked.",
            "  3. Pair developers on critical-path items to reduce single-person dependency.",
        ]
    else:
        lines += [
            "  1. Review workloads at sprint planning to prevent future imbalance.",
            "  2. Pair senior and junior developers on complex work items.",
        ]

    return "\n".join(lines)


def _answer_risks(context: str) -> str:
    """
    Simulator handler: answer risk, threat, and delay questions.

    Identifies the top 3 risks from: vendor/external blockers (hardcoded for
    the demo dataset), declining velocity trend, and key-person overload.
    Produces a structured risk table with severity ratings and action items.
    """
    vel    = _parse_velocity(context)
    people = _parse_allocation(context)
    # Sort by total + 2x blocked so the most impactful person ranks highest
    top    = sorted(people, key=lambda x: x["total"] + 2 * x["blocked"], reverse=True)
    o      = top[0] if top else {}
    avg    = round(sum(p["total"] for p in people) / len(people), 1) if people else 0

    lines = [
        "TOP 3 RISKS THIS SPRINT",
        "-" * 22,
        "",
        "  #   Severity   Risk                          Story Points at Stake",
        "  --- ---------  ----------------------------  ----------------------",
        "  1   HIGH       Vendor API delay              21 pts blocked (PROJ-020/021)",
        "  2   MEDIUM     Declining sprint velocity     Timeline buffer shrinking",
        f"  3   MEDIUM     Key-person overload           {o.get('total','?')} pts on one member",
        "",
        "Risk 1 -- Vendor API Delay  (PROJ-020, PROJ-021)",
        "  DataBridge Inc. has missed 3 follow-ups; 2-week minimum delay expected.",
        "  PROJ-021 (audit logging) is blocked until PROJ-020 API schema arrives.",
        "  Security audit deadline is 2026-04-10 -- no room for further slippage.",
        "  Action: Escalate to procurement by 2026-03-15 with a formal SLA demand.",
        "",
        f"Risk 2 -- Declining Velocity  {vel['history']}",
        f"  Sprint 4 delivered {vel['latest']} pts -- {abs(vel['trend_pct']):.0f}% below Sprint 1 ({vel['first']} pts).",
        f"  If trend holds, Sprint 5 may deliver only ~{vel['projected']} pts.",
        f"  At {vel['projected']} pts/sprint the 32-day deadline buffer shrinks significantly.",
        "  Action: Run Sprint 4 root-cause retro; surface blockers early in Sprint 5.",
        "",
    ]

    if o:
        remaining = o["total"] - o["done"]
        lines += [
            f"Risk 3 -- Key-Person Dependency  ({o['name']}, {o['role']})",
            f"  {o['total']} pts assigned vs team avg {avg} -- {round((o['total']-avg)/avg*100):+d}% above average.",
            f"  {o.get('blocked','?')} pts blocked + {o.get('in_progress','?')} pts in progress simultaneously.",
            f"  {remaining} pts of remaining work depends on a single team member.",
            "  Action: Redistribute at least one To-Do ticket this sprint.",
        ]

    return "\n".join(lines)


def _answer_bottleneck(context: str) -> str:
    """
    Simulator handler: identify the biggest bottleneck and explain how to unblock them.

    Ranks team members by (total + 2 * blocked) to surface whoever is both
    heavily loaded and has the most stalled work. Also identifies any external
    vendor dependency as a secondary bottleneck.
    """
    people = _parse_allocation(context)
    if not people:
        return "No allocation data available."

    avg_total = sum(p["total"] for p in people) / len(people)
    # Primary bottleneck: the person with the most total work + stalled work
    primary   = sorted(people, key=lambda x: x["total"] + 2 * x["blocked"], reverse=True)[0]
    pct_above = round((primary["total"] - avg_total) / avg_total * 100)
    todo_pts  = primary["total"] - primary["done"] - primary["in_progress"] - primary["blocked"]

    lines = [
        "BOTTLENECK ANALYSIS",
        "-" * 19,
        "",
        f"  Primary Bottleneck : {primary['name']}  ({primary['role']})",
        "",
        "  Load breakdown:",
        f"    Total assigned : {primary['total']} pts  ({pct_above:+d}% above team avg)",
        f"    Done           : {primary['done']} pts",
        f"    In progress    : {primary['in_progress']} pts  <-- active right now",
        f"    Blocked        : {primary['blocked']} pts  <-- stalled, creating downstream wait",
        f"    To-do          : {todo_pts} pts  <-- future sprints already claimed",
        "",
        "  Cascade:",
        "    PROJ-021 (Audit Logging) cannot start until PROJ-020 unblocks.",
        "    The security audit (2026-04-10) depends on both being resolved.",
        "",
        "  Secondary Bottleneck : External Vendor (DataBridge Inc.)",
        "    Blocking PROJ-020 -- 3 follow-ups sent with no response.",
        "",
        "How to Unblock:",
        f"  1. [Immediate]    Escalate DataBridge to procurement -- formal SLA demand.",
        f"  2. [This sprint]  Move one To-Do ticket from {primary['name'].split()[0]} to a lower-loaded member.",
        f"  3. [This sprint]  Stub the API contract so PROJ-021 design can start in parallel.",
        f"  4. [Next sprint]  Cap {primary['name'].split()[0]}'s new intake until blocked items clear.",
    ]
    return "\n".join(lines)


def _answer_pm_actions(context: str) -> str:
    """
    Simulator handler: what are the most important PM actions for the next 2 weeks?

    Builds a prioritised action table (P1-P5) with rationale for each item,
    derived from: blocked tasks, team load imbalance, velocity decline,
    on-track status, and external dependency status.
    """
    people = _parse_allocation(context)
    vel    = _parse_velocity(context)
    est    = _extract(context, "Est. End Date")
    target = _extract(context, "Target Date")
    track  = _extract(context, "On Track")
    buffer_days = _days_between(est, target) if "N/A" not in (est, target) else "?"

    # Most overloaded and most underutilised members for task redistribution advice
    o = sorted(people, key=lambda x: x["total"] + 2 * x["blocked"], reverse=True)[0] if people else {}
    u = sorted(people, key=lambda x: x["total"])[0] if people else {}

    lines = [
        "PM ACTION PLAN -- NEXT 2 WEEKS",
        "-" * 30,
        "",
        f"  {'#':<3} {'Priority':<10} {'Action':<48} {'By When'}",
        f"  {'-'*3} {'-'*10} {'-'*48} {'-'*12}",
        "  P1  CRITICAL   Escalate DataBridge -- procurement SLA demand       2026-03-15",
        f"  P2  HIGH       Redistribute ticket(s) off {o.get('name','overloaded member'):<19} This sprint",
        f"  P3  HIGH       Sprint 4 retro: root-cause velocity drop (22 pts)  ASAP",
        f"  P4  MEDIUM     Brief stakeholders -- project on track ({buffer_days}-day buffer) This week",
        "  P5  MEDIUM     Begin PROJ-021 design with stubbed API contract     This sprint",
        "",
        "Rationale:",
        "",
        "  P1  21 pts are blocked on a single external dependency.",
        "      The security audit (2026-04-10) cannot proceed without resolution.",
        "      A procurement escalation with SLA leverage is more effective than email follow-up.",
        "",
    ]

    if o and u:
        lines += [
            f"  P2  {o.get('name','?')} carries {o.get('total','?')} pts "
            f"({o.get('blocked','?')} blocked, {o.get('in_progress','?')} in progress).",
            f"      {u.get('name','?')} has {u.get('capacity','?')} h/week capacity "
            f"with only {u.get('total','?')} pts assigned -- clear capacity to absorb work.",
            "",
        ]

    lines += [
        f"  P3  Sprint velocity dropped {abs(vel['trend_pct']):.0f}% over 4 sprints ({vel['first']} -> {vel['latest']} pts).",
        "      Without diagnosing the cause, Sprint 5 may deliver similarly low output.",
        "",
        f"  P4  Project is {'on track' if 'YES' in track.upper() else 'AT RISK'} "
        f"(est. {est} vs target {target}).",
        "      Sharing this proactively reduces stakeholder pressure and builds trust.",
        "",
        "  P5  Stubbing the API contract decouples PROJ-021 from the vendor delay.",
        "      David can proceed with audit logging design now, saving ~1 sprint later.",
    ]
    return "\n".join(lines)


# ── Main simulator function ────────────────────────────────────────────────────

def simulate_response(context: str, question: str) -> str:
    """
    Rule-based LLM simulator.

    Generates structured, data-driven analytical answers from the project
    context. Used automatically when no real LLM backend is available.
    Can also be called directly for testing or offline demos.

    Routing is keyword-based; the first matching keyword group wins.
    The order matters: more specific handlers (bottleneck) are checked
    before broader ones (timeline, resources).

    Parameters
    ----------
    context  : str  Structured project context string.
    question : str  The user's question in natural language.

    Returns
    -------
    str  A plain-text structured answer.
    """
    q = question.lower()

    # Route to the most specific handler first
    if any(kw in q for kw in ["bottleneck", "slowdown", "hinder", "unblock"]):
        return _answer_bottleneck(context)
    if any(kw in q for kw in ["complet", "finish", "deadline", "when", "deliver", "on track", "schedul"]):
        return _answer_timeline(context)
    if any(kw in q for kw in ["workload", "balanc", "overload", "resource", "allocat", "capacity", "team"]):
        return _answer_resources(context)
    if any(kw in q for kw in ["top 3", "risk", "threat", "delay", "block", "concern"]):
        return _answer_risks(context)
    if any(kw in q for kw in ["action", "focus", "priorit", "pm", "manager", "this week", "important", "should"]):
        return _answer_pm_actions(context)
    if any(kw in q for kw in ["status", "overview", "summary", "progress", "health", "update"]):
        # Inline overview — simpler than the dedicated handlers above
        lines = [
            "PROJECT STATUS OVERVIEW",
            "-" * 23,
            f"  Completion : {_extract(context, 'Completion')}",
            f"  Remaining  : {_extract(context, 'Remaining Total')}",
            f"  Blocked    : {_extract(context, 'Blocked')}",
            f"  Est. End   : {_extract(context, 'Est. End Date')}",
            f"  Target     : {_extract(context, 'Target Date')}",
            f"  On Track   : {_extract(context, 'On Track')}",
            f"  Velocity   : {_parse_velocity(context)['history']}  "
            f"(avg {_parse_velocity(context)['avg']}, "
            f"trend: {'declining' if _parse_velocity(context)['trend_pct'] < -10 else 'stable'})",
        ]
        return "\n".join(lines)

    # Fallback: generic snapshot + example questions to guide the user
    vel   = _parse_velocity(context)
    risks = _extract_section(context, "IDENTIFIED RISKS")
    return (
        f"Project snapshot:\n"
        f"  Completion : {_extract(context, 'Completion')}\n"
        f"  On Track   : {_extract(context, 'On Track')}\n"
        f"  Velocity   : {vel['history']}  (avg {vel['avg']})\n"
        f"  Est. End   : {_extract(context, 'Est. End Date')}\n\n"
        "Top risks:\n" + ("\n".join(risks[:5]) or "  None identified.") + "\n\n"
        "Try one of the example questions:\n" +
        "\n".join(f"  - {q}" for q in EXAMPLE_QUESTIONS)
    )


# ── Backend routing ────────────────────────────────────────────────────────────

def _active_backend() -> str:
    """
    Detect which LLM backend is currently available.

    Checks backends in priority order and returns the name of the first one
    that is ready to use. The simulator is always available as a last resort.

    Returns
    -------
    str  One of: "ollama", "llama", "claude", "simulator"
    """
    if _ollama_is_running():
        return "ollama"
    if LLAMA_CPP_ENABLED:
        return "llama"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return "simulator"


def query_llm(context: str, question: str) -> str:
    """
    Ask a natural-language question about the project using the best available backend.

    Automatically selects the active backend (Ollama > llama.cpp > Claude > simulator),
    calls the appropriate query function, prints the answer to stdout (streaming
    for Ollama and Claude), and returns the complete answer as a string.

    Parameters
    ----------
    context  : str  Structured project context (built by the Responder or caller).
    question : str  The user's natural-language question.

    Returns
    -------
    str  The complete answer text from whichever backend responded.
    """
    backend = _active_backend()

    if backend == "ollama":
        # Check the model is downloaded before trying to use it
        if not _ollama_has_model():
            print(f"[!] Model '{OLLAMA_MODEL}' not found. Run: ollama pull {OLLAMA_MODEL}")
            print("[!] Falling back to simulator.\n")
            answer = simulate_response(context, question)
            print(answer)
            return answer
        return _query_ollama(context, question)

    if backend == "llama":
        answer = _query_llama(context, question)
        print(answer)
        return answer

    if backend == "claude":
        return _query_claude(context, question)

    # Default fallback: rule-based simulator (no LLM required)
    answer = simulate_response(context, question)
    print(answer)
    return answer


def active_backend_label() -> str:
    """
    Return a human-readable label describing the currently active LLM backend.

    Used by main.py and the web UI to display which backend is in use.
    For Ollama, also shows whether the configured model has been pulled.
    """
    backend = _active_backend()
    if backend == "ollama":
        has = _ollama_has_model()
        return f"Ollama  (model: {OLLAMA_MODEL})" if has else f"Ollama  (model '{OLLAMA_MODEL}' not pulled yet)"
    return {
        "llama":     f"llama.cpp  ({MODEL_PATH.name})",
        "claude":    f"Claude API ({CLAUDE_MODEL})",
        "simulator": "rule-based simulator  (no LLM — install Ollama for real inference)",
    }[backend]
