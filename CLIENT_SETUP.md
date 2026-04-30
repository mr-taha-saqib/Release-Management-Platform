# How to Set Up and Use the App
### Complete Step-by-Step Guide — No Technical Knowledge Needed

---

## Technologies Used (Backend)

| What | Technology | Why |
|---|---|---|
| Web server | **FastAPI + Uvicorn** | Runs the app at http://localhost:8000 |
| Terminal interface | **Python** (built-in) | CLI mode, no extra install needed |
| Jira connection | **Jira REST API + requests** | Pulls your tasks from Jira Cloud |
| Confluence connection | **Confluence REST API** | Pulls your docs (optional) |
| Local AI | **Ollama** (optional, free) | Runs AI models on your computer |
| Cloud AI | **Claude API** (optional) | Anthropic AI, needs API key + internet |
| Fallback engine | **Built-in rule-based** | Always works, no setup needed |
| Credentials | **python-dotenv** | Reads your settings from a `.env` file |

---

## What is this app?

This app connects to your Jira account, reads your project tasks, and lets you ask plain-English questions like:

- "When will this project be finished?"
- "Who on the team is overloaded?"
- "What are the biggest risks right now?"
- "What should we focus on next?"

You type a question, it gives you a smart answer. All your data stays on your own computer — nothing is sent anywhere.

---

# PART 1 — INSTALLATION
### (Do this once. You never have to do it again.)
### Total time: ~20–30 minutes

---

## STEP 1 — Install Python *(~5 minutes)*

Python is a free program that runs the app. You probably don't have it yet.

1. Open your browser and go to: **https://www.python.org/downloads/**
2. Click the big yellow **"Download Python"** button
3. Open the file that downloads to your computer
4. **VERY IMPORTANT:** On the first screen of the installer, you will see a small checkbox at the bottom that says **"Add Python to PATH"** — **tick this box** before clicking anything else
5. Click **"Install Now"**
6. Wait for it to finish, then click **"Close"**

**Check it worked:**
- Press the **Windows key** on your keyboard
- Type `cmd` and press **Enter** — a black window opens (this is called Command Prompt)
- Type the following and press Enter:

```
python --version
```

You should see something like `Python 3.13.0`. If you do — Python is installed. Move to Step 2.

> If you see `python is not recognized` — go back and reinstall Python, making sure to tick the "Add Python to PATH" box.

---

## STEP 2 — Open the Project Folder *(~1 minute)*

1. Press the **Windows key**, type `cmd`, press **Enter** — the black Command Prompt window opens
2. Type this exactly and press **Enter**:

```
cd d:\llmproject
```

You are now inside the project folder. **Keep this window open** — you will use it for the rest of the setup.

---

## STEP 3 — Install the App's Requirements *(~3 minutes)*

In the Command Prompt window, type this and press **Enter**:

```
pip install -r requirements.txt
```

You will see a lot of text scrolling — that is normal. It is downloading small tools the app needs. When it stops and the cursor (`>`) appears again, it is done.

---

## STEP 4 — Create Your Settings File *(~10 minutes)*

The app needs your Jira login details. We store these in a file called `.env` (a simple text file).

**4a. Create the file:**

In the Command Prompt, type this and press Enter:

```
copy .env.example .env
```

**4b. Open the file:**

1. Open **File Explorer** (the folder icon on your taskbar)
2. Go to: `d:\llmproject`
3. Find the file named **`.env`**
4. Right-click it → **Open with** → **Notepad**

You will see this:

```
ATLASSIAN_EMAIL=your.email@example.com
ATLASSIAN_API_TOKEN=your-api-token-here
ATLASSIAN_DOMAIN=https://yourcompany.atlassian.net
JIRA_PROJECT_KEY=MYPROJ
JIRA_DEADLINE=2026-12-31
```

Now fill in each line. Here is exactly what to put:

---

### ATLASSIAN_EMAIL

Your Jira login email address.

```
ATLASSIAN_EMAIL=john@mycompany.com
```

---

### ATLASSIAN_DOMAIN

The website address you use to open Jira in your browser. Copy it from your browser's address bar when Jira is open.

```
ATLASSIAN_DOMAIN=https://mycompany.atlassian.net
```

> Do not add a `/` at the end.

---

### ATLASSIAN_API_TOKEN

This is a special password the app uses to connect to Jira on your behalf. Here is how to get it:

1. Open your browser and go to:
   **https://id.atlassian.com/manage-profile/security/api-tokens**
   (Log in to your Atlassian account if asked)
2. Click **"Create API token"**
3. A small box appears — type any name like `LLM App` and click **"Create"**
4. A long code appears — **copy it immediately** (you cannot see it again after closing this window)
5. Paste it into the `.env` file

```
ATLASSIAN_API_TOKEN=ATATT3xFfGF0abc123yourtokenhere
```

---

### JIRA_PROJECT_KEY

The short code that appears at the start of your Jira issue numbers. For example, if your tasks are called `PROJ-1`, `PROJ-2`, etc., the key is `PROJ`.

**How to find it in Jira:**
1. Open your project in Jira
2. Click **Project Settings** in the left sidebar (near the bottom)
3. Click **Details**
4. Look for the word **Key** — that value is what you need

```
JIRA_PROJECT_KEY=PROJ
```

---

### JIRA_DEADLINE

The target finish date for your project. Use the format `YYYY-MM-DD`.

```
JIRA_DEADLINE=2026-12-31
```

If you are not sure, leave the date as a rough estimate — you can change it later.

---

**4c. Save and close Notepad:**

Press **Ctrl+S** to save, then close Notepad.

---

## STEP 5 — Test Your Connection *(~2 minutes)*

Go back to Command Prompt and type:

```
python setup_check.py
```

This checks if everything is set up correctly. A good result looks like this:

```
[PASS]  .env file found
[PASS]  ATLASSIAN_EMAIL = john@mycompany.com
[PASS]  Connected to Jira as: John Smith
[PASS]  Project found: 'My Project'
```

If you see any `[FAIL]` lines — check the matching section in Step 4, fix the value in your `.env` file, and run this command again. Keep fixing until you see all green `[PASS]` lines.

---

## STEP 6 — Pull Your Data From Jira *(~1 minute)*

Now load your actual Jira tasks into the app. Type:

```
python connectors/sync.py
```

This connects to Jira, downloads your project tasks, and saves them on your computer. You will see:

```
SYNC COMPLETE
Projects synced: 1
Issues: 47  |  Team: 5  |  File: data/project_PROJ.json
```

> Run this command again any time you want to refresh with the latest tasks from Jira.

---

## STEP 7 — Start the App *(~30 seconds)*

Type this and press Enter:

```
python app.py
```

You will see:

```
Uvicorn running on http://0.0.0.0:8000
```

The app is now running. **Open your browser and go to:**

## http://localhost:8000

---

# PART 2 — USING THE APP
### (What you see in the browser and how to use it)

---

## The Main Screen

When you open http://localhost:8000 you will see:

**At the top:**
A row of coloured boxes showing your project stats at a glance:
- **Project name**
- **Completion %** — how much of the work is done, with a progress bar
- **Risk Level** — LOW / MEDIUM / HIGH / CRITICAL (colour coded)
- **Buffer** — how many days of breathing room before the deadline
- **Blocked Tasks** — tasks stuck waiting for something
- **Projected Finish** — estimated completion date (green = on track, red = at risk)

**In the middle:**
A question panel where you interact with the app.

---

## How to Ask a Question

**Option A — Use a preset question (easiest):**

You will see 6 grey buttons with ready-made questions:
- "Give me a full project overview"
- "When will the project be completed?"
- "What are the main risks?"
- "Are there any blocked tasks?"
- "Who is overloaded on the team?"
- "What should we prioritise next?"

Click any button — it fills in the question and sends it automatically. The answer appears below within a few seconds.

**Option B — Type your own question:**

Click in the text box that says **"Or type your own question..."**, type your question, then either:
- Press **Enter** on your keyboard, or
- Click the blue **"Ask"** button

---

## How to Switch Between Projects

If you have more than one project synced from Jira:

1. Look for the **dropdown box** at the top of the question panel — it shows your current project name
2. Click it — a list of all your projects appears
3. Click the project you want — the stats at the top update automatically
4. Now ask questions — they will be about the selected project

---

## How to Refresh Data From Jira (Without Restarting)

If your team has updated tasks in Jira and you want the latest data:

1. Look at the top of the page for the green **"Sync from Jira"** button
2. Click it
3. Wait a few seconds — you will see `Synced: 47 issues (My Project)`
4. The page reloads automatically with fresh data

> This does the same thing as running `python connectors/sync.py` in the Command Prompt, but right from the browser.

---

## Example Questions You Can Ask

Here are more questions you can type:

| What you want to know | Question to ask |
|---|---|
| Overall health of the project | "Give me a full project overview" |
| Will we hit the deadline? | "When will the project be completed?" |
| What's going wrong? | "What are the main risks?" |
| Who is stuck or blocked? | "Are there any blocked tasks?" |
| Is the team balanced? | "Who is overloaded on the team?" |
| What to do this week | "What should we prioritise next?" |
| Who is causing delays | "Who is the biggest bottleneck?" |
| What actions should I take | "What are the most important actions for me this week?" |

---

# PART 3 — DAILY USE
### (What to do every day / week)

---

## Starting the App Each Day

Every time you want to use the app:

1. Open Command Prompt (Windows key → type `cmd` → Enter)
2. Type `cd d:\llmproject` and press Enter
3. Type `python app.py` and press Enter
4. Open your browser and go to **http://localhost:8000**

That's it — the app is ready.

---

## Refreshing Your Jira Data

If you want the latest tasks from Jira before asking questions, click the green **"Sync from Jira"** button on the main page — or run:

```
python connectors/sync.py
```

You do not have to do this every day — only when you want updated information.

---

## Stopping the App

When you are done using the app:

1. Go to the Command Prompt window where the app is running
2. Press **Ctrl + C**

The app stops. Your browser page will no longer work until you start it again.

---

# PART 4 — OPTIONAL: BETTER AI ANSWERS WITH OLLAMA
### (Free, runs on your computer, gives much richer answers)

Right now the app uses a built-in rule-based engine. It works well, but you can get smarter, more conversational answers by installing **Ollama** — a free tool that runs an AI model on your own computer. No internet needed, no subscription, no data sent anywhere.

---

## Install Ollama

1. Go to: **https://ollama.com**
2. Click **"Download"** and install it like any normal Windows program
3. Once installed, open Command Prompt and type:

```
ollama pull llama3.2
```

This downloads an AI model (~2 GB). Wait for it to finish.

4. Ollama runs quietly in the background automatically — you do not need to start it manually.

---

## Connect the App to Ollama

The app detects Ollama automatically. Just:

1. Stop the app if it is running (Ctrl+C in Command Prompt)
2. Start it again: `python app.py`
3. Open http://localhost:8000

The badge at the top of the page will now show **"Ollama (model: llama3.2)"** instead of "Rule-based AI". Answers will now come from the local AI model.

---

# PART 5 — MULTIPLE JIRA PROJECTS

If you want to load more than one Jira project:

1. Open the `.env` file in Notepad (same as Step 4)
2. Find the line `JIRA_PROJECT_KEY=PROJ`
3. Replace it with a new line that lists all your project keys separated by commas:

```
JIRA_PROJECT_KEYS=PROJ,MOB,CRM
```

4. Save the file
5. Run the sync again:

```
python connectors/sync.py
```

All three projects will be downloaded. You can then switch between them using the dropdown on the app's main page.

---

# PART 6 — SOMETHING WENT WRONG?

| What you see | What to do |
|---|---|
| `python is not recognized` | Reinstall Python — make sure to tick **"Add Python to PATH"** |
| `[FAIL] 401 Unauthorized` | Wrong email or expired token — go to id.atlassian.com and create a new API token |
| `[FAIL] Project not found` | Check `JIRA_PROJECT_KEY` in `.env` — must match exactly (e.g. `PROJ` not `proj`) |
| `[FAIL] Cannot connect to Jira` | Check your internet and that `ATLASSIAN_DOMAIN` is correct in `.env` |
| Browser shows nothing at localhost:8000 | Make sure `python app.py` is still running in Command Prompt |
| App shows no projects in dropdown | Run `python connectors/sync.py` first — you need to pull data before the app can show anything |
| "Sync from Jira" button shows an error | Run `python setup_check.py` in Command Prompt — it will tell you exactly what is wrong |
| App shows "Rule-based AI" not Ollama | Ollama is not running or model not pulled — follow Part 4 above |

---

**Still stuck?** Note down exactly what the screen says and share it — we will fix it together on the call.
