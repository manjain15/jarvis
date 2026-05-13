# Jarvis Setup Guide
Complete this once and Jarvis runs forever.

---

## What you need

- A Gmail account (you already have one)
- An Anthropic API key (~$0.01–0.03 per brief)
- Python 3.9+ installed
- 20 minutes

---

## Step 1 — Get your Anthropic API key

1. Go to https://console.anthropic.com
2. Sign up or log in
3. Click **API Keys** → **Create Key**
4. Copy the key (starts with `sk-ant-api03-...`)
5. Paste it into `config.py` → `ANTHROPIC_API_KEY`

---

## Step 2 — Set up Google Cloud credentials

This is the only fiddly part. You're creating a "project" in Google Cloud
that has permission to read your Gmail and Calendar. It's free.

### 2a. Create a Google Cloud project

1. Go to https://console.cloud.google.com
2. Click the project dropdown (top left) → **New Project**
3. Name it "Jarvis" → **Create**

### 2b. Enable the APIs

1. In your new project, go to **APIs & Services** → **Library**
2. Search for **Gmail API** → Enable it
3. Search for **Google Calendar API** → Enable it

### 2c. Create OAuth credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. If prompted, configure the OAuth consent screen first:
   - User Type: **External**
   - App name: "Jarvis"
   - Your email as support contact
   - Add your email under **Test users**
   - Save and continue through the rest
4. Back in Credentials → **Create Credentials** → **OAuth client ID**
   - Application type: **Desktop app**
   - Name: "Jarvis Desktop"
   - Click **Create**
5. Click **Download JSON**
6. Rename the downloaded file to `credentials.json`
7. Move it into your Jarvis folder (same folder as `morning_brief.py`)

---

## Step 3 — Install Python dependencies

Open a terminal in your Jarvis folder and run:

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 \
            google-api-python-client anthropic pytz
```

---

## Step 4 — Edit config.py

Open `config.py` and fill in:
- `YOUR_EMAIL` — your Gmail address
- `ANTHROPIC_API_KEY` — from Step 1

---

## Step 5 — Copy your profile document

Copy your `jarvis_profile.md` file into the Jarvis folder.
Rename it to `profile.md`.

---

## Step 6 — Authenticate with Google (one time only)

```bash
python morning_brief.py --setup
```

This opens a browser window. Log in with your Google account,
click through the permissions, and close the browser when done.
A `token.json` file is saved — you won't need to do this again.

---

## Step 7 — Send your first test brief

```bash
python morning_brief.py --test
```

Check your inbox. Your Jarvis brief should arrive within 30 seconds.

---

## Step 8 — Schedule it at 7am daily

```bash
python morning_brief.py --schedule
```

This prints the exact cron command or Task Scheduler instructions for your system.

---

## Troubleshooting

**"credentials.json not found"**
→ Make sure the file is in the same folder as `morning_brief.py`

**"Access blocked: This app's request is invalid"**
→ Add your email as a Test User in the OAuth consent screen (Step 2c)

**"ANTHROPIC_API_KEY is invalid"**
→ Check the key in config.py — make sure there are no spaces or quotes missing

**Brief arrives but looks wrong**
→ Update your `profile.md` — the brief is only as good as the profile

---

## File structure when fully set up

```
jarvis/
├── morning_brief.py     ← main script
├── config.py            ← your settings
├── profile.md           ← your Jarvis profile document
├── credentials.json     ← from Google Cloud (Step 2)
├── token.json           ← auto-created after --setup
├── SETUP_GUIDE.md       ← this file
└── jarvis.log           ← auto-created, logs each run
```

---

## Cost estimate

Each morning brief calls the Claude API once.
At current pricing, each brief costs roughly **$0.01–0.03**.
That's under $1/month to have a personalised AI briefing every day.

---

## Updating your profile

Your brief gets smarter the more you maintain `profile.md`.
Set a reminder to update it once a week — especially the **Active Projects** table.
The more accurate it is, the more Jarvis feels like it actually knows you.
