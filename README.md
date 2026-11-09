# Cora

**Conversation Optimized Relational Assistant**

Cora is a voice and text assistant with persistent memory and a life of her own: interests
she explores, desires, a self that grows from experience, and a relationship with you built
from how your conversations go. She runs as a terminal app on your PC, and you can also
reach her on Signal, by email, from a web page on your phone, or by phone call. She can
message, email and call people for your errands, always with your approval.

How it all works is in [DOCUMENTATION.md](DOCUMENTATION.md); what's next is in
[ROADMAP.md](ROADMAP.md).

## Getting started

Cora runs directly on Windows; only its PostgreSQL/pgvector database runs in Docker.
You need Python 3.12 or later with a virtual environment, Docker Desktop using Linux containers,
an OpenAI API key, and a microphone/speaker for voice interaction.

### 1. Install and configure

```powershell
git clone https://github.com/Nixxs/cora.git
cd cora
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.database.example .env.database
```

Run the commands below from the repository root with the environment activated
(`.venv\Scripts\Activate.ps1` in each new terminal). If PowerShell refuses to run the
activation script, allow local scripts once with
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

Only copy the example on a fresh setup; keep an existing database configuration.
Cora asks for your OpenAI API key the first time she starts, checks it with OpenAI and keeps
it in the Windows credential store; you can replace it later in Settings > General.
(`OPENAI_API_KEY=your-key` in a local `.env` file also works; a key saved in the app wins.)
In `.env.database`, choose a private database password **before storing personal
conversations**. Both settings files are ignored by Git; never commit credentials.

The database defaults to `127.0.0.1:5432`, with database and user named `cora`.
Change `CORA_DB_PORT` if that port is already in use. Keep the database local.

### 2. Start the database and apply migrations

```powershell
docker compose --env-file .env.database up -d --wait database
python migrate.py
```

Migrations are explicit: starting Cora does not apply them. Rerunning the migration
command safely skips already-applied files.

### 3. Run Cora

```powershell
python app.py
```

Keep Docker running while chatting, and run **only one Cora instance at a time**.
The startup check treats other active conversations as interrupted.

Each launch rewrites a diagnostic `live-trace.log` beside `app.py` (IDs and state only,
never chat text or audio; ignored by Git), so keep a copy before restarting if you need it.

To stop the database afterwards:

```powershell
docker compose --env-file .env.database down
```

**Your data lives in a persistent Docker volume, but this is not a backup.**
Do not use `down -v` / `down --volumes`, delete the volume, or reset Docker's data:
these can erase saved conversations. Changing settings after initial setup does
not change an existing database's password or user.

### Backups

Everything that makes Cora who she is (conversations, memories, identity and its
history, interests, your relationship, people, settings) is in the database
volume, inside Docker Desktop's virtual machine, not a folder you can copy. Back
it up with [backup.py](backup.py):

```powershell
python backup.py                          # back up now: database and Signal keys
python backup.py --list                   # the backups there are
python backup.py restore backups\cora-database-2026-10-26_211101.dump
```

- The database backup is a `pg_dump` snapshot, consistent while the database
  runs and restorable onto any fresh install (a few MB). Her Signal keys are
  archived with signal-cli paused for a moment; without them you only re-link
  her phone.
- Backups go in `backups/` (ignored by Git), keeping the newest 14 of each.
  **They hold every private conversation**: keep them private.
- **Automatic:** at launch, if the newest database backup is more than a day old,
  Cora backs up the database in the background and notes it in Activity (Signal
  keys only with the manual command, so Signal is never paused while you use her).
- **A second copy:** in Settings > General > Backups, choose a folder on another disk
  (it must already exist), and every backup is copied there too, pruned the same way.
  The command-line backup uses it as well. A cloud folder
  works, but uploads your conversations to that service.
- **Restoring** replaces the whole database with the backup. Quit Cora first; it
  asks you to type `restore`, and saves the current database as a backup before
  replacing it, in case the wrong file was chosen. Then start Cora as usual.

## Configuration

Settings that live in `.env` (restart Cora to apply a change). Everything else is set in the
app's **Settings**. Automatic standby, her goodbye before it, the wake word, when a
conversation ends, the second backup folder and the Twilio region moved to Settings; their
old `.env` lines (`CORA_IDLE_TIMEOUT_SECONDS`, `CORA_STANDBY_FAREWELL`, `CORA_WAKE_ENABLED`,
`CORA_CONVERSATION_GAP_MINUTES`, `CORA_BACKUP_COPY_DIR`, `CORA_TWILIO_API`) still apply until
you change them there, then can be deleted.

| Setting | Default | What it does |
| --- | --- | --- |
| `OPENAI_API_KEY` | (none) | Your OpenAI API key, if you'd rather not enter it in the app; one saved in Settings > General wins |
| `CORA_WAKE_DEBUG` | `1` | Wake diagnostics in Activity (what Whisper heard, a heartbeat); `0` hides them |
| `CORA_EMAIL_CLIENT_ID`, `CORA_EMAIL_TENANT` | (none) | Fallbacks for the email app ID and directory, normally saved in Settings |

In `.env.database`: `CORA_DB_PORT` (default `5432`) and `CORA_SIGNAL_PORT` (default `8090`).

## Local wake-word setup

"Hey Cora" is detected on your PC with a local Whisper model (faster-whisper, CPU, INT8);
no room audio goes to OpenAI while she listens in Standby.

1. Download `model.bin`, `config.json`, `tokenizer.json` and `vocabulary.txt` from
   [Systran/faster-whisper-base.en](https://huggingface.co/Systran/faster-whisper-base.en/tree/main)
   into `models/wake/faster-whisper-base.en/` (ignored by Git). Nothing downloads by itself;
   missing files stop the listener with an Activity error.
2. Click **Wake off** in the bottom row to turn it on. It stays as you left it at the next
   launch.
3. Enter Standby and say "Hey Cora" (or Kora, Korra). With `CORA_WAKE_DEBUG=1`, Activity
   shows what Whisper heard and a heartbeat every five seconds, to check the microphone is
   being heard.

How detection works is in [Standby](DOCUMENTATION.md#standby).

## Setting up services

Each service is optional, off until you set it up, and found under **Settings** (top border)
› **Services**. How each one works is in [DOCUMENTATION.md](DOCUMENTATION.md#services).
Tokens and keys you enter go to the system credential store (Windows Credential Manager),
never to a file or the database. Run `python migrate.py` after updating, as always.

### GitHub

1. On GitHub, create a fine-grained personal access token (Settings › Developer settings ›
   Fine-grained tokens), read-only, for the repositories Cora may see: Contents, Issues, Pull
   requests and Metadata are enough to start.
2. In Settings › Services › **GitHub**, paste it under Access token and press **Save**.
   **Forget** removes it.
3. Switch the service on. Click its row to see its tools, and switch on the ones you want her
   to have (all start off). Tools that change things ask you each time.

### Browser

The browser runs in Docker (Microsoft's Playwright MCP server, from
[compose.yaml](compose.yaml)), so Docker must be running. No token is needed.

1. Optionally fetch the image ahead (about 1.5 GB; otherwise the first start takes a few
   minutes): `docker compose --env-file .env.database --profile browser pull`
2. Switch **Browser** on in Settings › Services.
3. Switch on its tools: at least `browser_navigate` and `browser_snapshot` to read pages. For
   searching sites, filling in forms for you and signing up to sites, also `browser_click`,
   `browser_type` and `browser_select_option`.

If it fails to start, its log is at `%LOCALAPPDATA%\cora\services\playwright.log`.

### Signal

Cora gets her own Signal account: **her own number on her own phone, never yours**.

1. Put Signal on a spare phone (or a second SIM) with Cora's own number.
2. In Settings › Services › **Signal**, switch it on. Cora starts the `signal-cli` container
   and waits for it.
3. Enter **your own** number and save it: she answers you, and the people you add.
4. Press **Link Cora's phone** and, on **Cora's** phone, open Signal › Settings › Linked
   devices › + and scan the QR code within five minutes. The row then shows her number.
   Never scan it with your own phone: if you do, Cora notices, removes the link, and asks you
   to remove "Cora" from your Linked devices.
5. Optionally add people under "People she may talk with": a name and their Signal number.
   Their **Calls** button (off by default) lets them phone her once phone calls are set up.

Her Signal keys live in the `signal-data` Docker volume: don't delete it, and back it up
(`python backup.py`). Losing it means linking her phone again.

### Email and calendar

Cora gets her own Outlook mailbox and calendar (work, school or personal), never yours.

1. Make a mailbox for Cora.
2. In the [Azure portal](https://portal.azure.com), register an app (Microsoft Entra ID › App
   registrations › New registration). Name it "Cora", leave the redirect URI empty, and for
   **Supported account types** choose:
   - work or school: **Accounts in this organizational directory only** (signed in to the
     portal with an account of that organisation);
   - personal: **Personal Microsoft accounts only**.

   Under **Authentication**, set **Allow public client flows** to **Yes** and save. No client
   secret is needed.
3. In Settings › Services › **Email**, paste the **Application (client) ID**. For a work or
   school account, also enter the organisation's domain or **Directory (tenant) ID**; leave it
   empty for a personal account. Press **Save**.
4. Press **Sign Cora in**, open the link, enter the code and sign in **as Cora's account**. It
   asks once to read and send her mail and read and change her calendar. A work directory may
   need an administrator to grant consent (the app's **API permissions** › **Grant admin
   consent**). The row then shows her address.
5. Optional, in the same section:
   - **Your address**, so you can email her yourself;
   - **Her signature** (Outlook doesn't add one to mail sent through Graph);
   - **Her name**, the full name she chose (such as "Cora Vale");
   - **Her sign-up password**, one password (12+ characters, not her Microsoft one) she uses
     for accounts on sites; it also needs the Browser.

**Sign out** forgets her sign-in. If the calendar says it needs her **signed in once more**
(a sign-in from before the calendar), press **Sign Cora in** again; if your app registration
lists API permissions, add Microsoft Graph's delegated `Calendars.ReadWrite`.

### Webhooks (the public address)

Webhooks, the web interface and phone calls all need Cora reachable over public HTTPS. She
listens only on this PC, at `127.0.0.1` on a local port (8000 by default), and ships no proxy:
use any reverse proxy or tunnel. With Caddy, for example:

```
cora.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

1. In Settings › Services › **Webhooks**, enter the public address (such as
   `https://cora.example.com`, no path) and the local port, and press **Save**.
2. Switch webhooks on for Microsoft to tell her about new mail and calendar changes straight
   away (it needs her email set up). The row shows each subscription and when it runs to.
   **New secrets** replaces the secret addresses if you ever need to.

### Web interface

1. Save the public address under **Webhooks** first.
2. Switch on Settings › Services › **Web interface**.
3. Choose **Pair a device**. A QR code and a short code show for five minutes:
   - on a phone or tablet, scan the QR code with the camera;
   - on a laptop, go to the address and type the code.

   On an iPhone or iPad, open the address in Safari, add it to the Home Screen (Share › Add to
   Home Screen) and pair from inside that app, since it keeps its own sign-in.
4. Paired devices are listed in the same row; **Remove** unpairs one at once.
5. Optional, for maps on the page: in Google Cloud Console (APIs & Services › Credentials ›
   Create credentials › API key), make a key and restrict it to **Websites** ›
   `https://cora.example.com/*` and the **Maps Embed API** only (enable it in the Library
   first; it's free). Paste it under **Google Maps key**.

If a CDN such as Cloudflare proxies the address, have it **bypass its cache** for that
hostname (Cloudflare: Caching › Cache Rules, "Hostname equals …" → Bypass cache) and purge it
once, or an update can leave devices with old files.

### Phone calls

Calls reach Cora through a Twilio number. You need the public address (under Webhooks) and
your own number (under Signal).

1. Sign up at [twilio.com](https://www.twilio.com) and upgrade from the trial (trial calls
   play a message first and only reach verified numbers; a call from any other number fails
   with error 21264).
2. Buy a number (Phone Numbers › Buy a number). Australian numbers need a Regulatory Bundle
   (an address and ID) that Twilio approves in a few days; a mobile number looks most like a
   person's. Move its voice to your nearest region (the number's page › Voice › active region,
   such as Australia AU1): otherwise every word travels to the US and back, which a
   conversation hears.
3. In the number's Voice configuration, set "A call comes in" to Webhook, **HTTP POST**, the
   address Settings shows (`https://cora.example.com/phone/incoming`).
4. Copy the auth token **for the number's region** (Console › API keys & tokens, with the
   region set to match; Live credentials › Auth Token). Each region has its own: the wrong one
   makes every call fail with 403 / error 11200.
5. In Settings › Services › **Phone calls**, paste the token and switch Phone calls on.
6. Call the number: Activity should say you're calling, and she answers. If it says a stranger
   was turned away, your number isn't coming through as the one under Signal (Twilio's Calls
   Log shows the number it got).
7. For calling out (she calls places for tasks, asking you first each time), enter the Account
   SID (Console › Account info, `AC…`) and her Twilio number (`+61…`) under Phone calls › Calling
   out and press **Save**. If the account isn't in Sydney (AU1), choose its **Twilio region**
   below.

If Cloudflare's bot protection covers the address, check Twilio's requests aren't challenged
(Security › Events). Forwarding a SIM's calls to the Twilio number is possible on some
carriers (`**21*<number>*11#`), but not all complete it; calling the Twilio number directly
always works.

## Documentation

- [DOCUMENTATION.md](DOCUMENTATION.md): how Cora works, from her memory and inner life to each
  service.
- [ROADMAP.md](ROADMAP.md): what's next, and the plans behind it.
