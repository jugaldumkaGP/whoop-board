# whoop-board

A personal WHOOP dashboard that runs entirely on your own Mac. `whoop.py` pulls your recovery, strain, sleep and workouts from the WHOOP API and writes them into `dashboard.html`, which you open straight from disk. No website, server, domain or hosting needed.

## What you need

- **An active WHOOP membership.** The API only returns data for an account with a strap on it. Without one, everything below still works, but every response comes back empty.
- **Python 3** (macOS ships with it; check with `python3 --version`). No packages to install.
- **About ten minutes.** Most of that is creating the WHOOP developer app.

## 1. Create a WHOOP developer app

1. Go to [developer-dashboard.whoop.com](https://developer-dashboard.whoop.com) and sign in with your WHOOP account.
2. Create a **Team**. WHOOP won't let you create an app until you have one.
3. Create an **App**. It asks for a name, a contact email, a privacy policy URL and a redirect URL. The last two look like they need a website. They don't:

### Privacy policy URL

Write a short, honest policy in a Google Doc, for example:

> This app reads my own WHOOP data for my personal use only.
> The data is stored on my own computer and is not shared with anyone.
> Nothing is sold, sent to third parties, or used for advertising.
> Contact: you@example.com

Then use **File → Share → Publish to web** and paste that published link. Don't use a normal share link: it can show WHOOP a "Request access" page instead of your policy.

### Redirect URL

Enter exactly:

```
https://localhost:8080/callback
```

WHOOP requires `https`. Plain `http://localhost` is rejected. Nothing needs to run at that address (see step 3).

### Scopes

Select all of these:

```
read:recovery  read:cycles  read:sleep  read:workout  read:profile  read:body_measurement  offline
```

`offline` is what lets the script refresh its own sign-in, so you only authorize once.

Save the app. You now have a **Client ID** and a **Client Secret**. Treat the secret like a password.

## 2. Add your credentials

```sh
git clone https://github.com/jugaldumkaGP/whoop-board.git
cd whoop-board
cp .env.example .env
```

Open `.env` and fill in `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET`. Leave the token lines empty: `whoop.py` fills them in. `.env` is git-ignored, so it is never committed.

## 3. Sign in to WHOOP (once)

```sh
python3 whoop.py --auth
```

1. The script prints a WHOOP sign-in link. Open it, log in and approve.
2. The browser then tries to load `https://localhost:8080/callback?code=…` and fails. **That's expected.** Nothing runs on localhost; the code you need is in the address bar.
3. Copy the **whole address** from the address bar and paste it into the terminal.

The script swaps the code for tokens, saves them to `.env` and pulls your full history. After this it refreshes the tokens itself. You only need `--auth` again if the refresh token stops working (the dashboard and `sync.log` will say so).

## 4. Open the dashboard

Open `dashboard.html` in your browser (double-click it, or `open dashboard.html`). It shows the data from the last pull.

## 5. Keep it updated

Pull new data any time:

```sh
python3 whoop.py          # last 14 days, plus a full re-pull once a day
python3 whoop.py --full   # force a full re-pull
```

Or let macOS pull in the background:

```sh
python3 whoop.py --install      # every 15 minutes (use --install 10 for every 10)
python3 whoop.py --status       # is it on, when did it last pull, any errors
python3 whoop.py --uninstall    # turn it off
```

This uses a `launchd` job (`~/Library/LaunchAgents/com.whoopdash.sync.plist`). It starts the script on a timer, so nothing listens on a port. An open dashboard tab re-reads `whoop_live.js` every minute, so new data shows up without reloading.

## Files

| File | What it is | In git? |
| --- | --- | --- |
| `whoop.py` | Sign-in, sync and the background job | yes |
| `dashboard.html` | The dashboard; `whoop.py` writes your data into it | yes, with the data block blanked |
| `embed_data.py` | Copies `whoop_data.json` into `dashboard.html` by hand (`whoop.py` already does this on every pull) | yes |
| `.env.example` | Template for `.env` | yes |
| `.env` | Client ID/secret and tokens | **no** |
| `whoop_data.json` | Your raw WHOOP data, including your email | **no** |
| `whoop_live.js` | Latest data for an open dashboard tab | **no** |
| `sync.log`, `.whoop.lock` | Sync log and run lock | **no** |

## Keeping your data out of git

`.gitignore` covers your credentials and data files. `dashboard.html` is trickier because `whoop.py` writes your data into it. A git filter blanks that data block on every commit, so the committed file has `{}` while your local copy keeps your data.

The filter has to be set up once in each clone, **before your first commit**:

```sh
git config filter.whoop-data.clean "perl -0pe 's#(<script type=\"application/json\" id=\"whoop-data\">).*?(</script>)#\${1}{}\${2}#s'"
git config filter.whoop-data.smudge cat
git config filter.whoop-data.required true
```

To check it's working, run `git show :dashboard.html | grep 'id="whoop-data"'` after staging. It should print `{}` between the tags.

## Troubleshooting

- **Everything comes back empty:** the WHOOP account has no active membership or strap.
- **"WHOOP_CLIENT_ID is missing":** fill in `.env` (step 2).
- **"No WHOOP tokens in .env yet" or "HTTP 401 … the sign-in has expired":** run `python3 whoop.py --auth` again.
- **"State didn't match":** you pasted an address from an older sign-in attempt. Run `--auth` again and use the newest one.
- **Redirect URL rejected when creating the app:** it must start with `https://`.

## License

MIT, see [LICENSE](LICENSE).
