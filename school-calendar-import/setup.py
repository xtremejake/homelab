#!/usr/bin/env python3
"""
One-command setup for the school calendar importer.

Run this on your own machine (it opens a browser twice: once for Google,
once for GitHub if needed):

    python setup.py [--repo xtremejake/homelab] [--trigger-dry-run]

What it does:
  1. Asks for your Gemini API key (hidden input).
  2. Runs Google OAuth in your browser -> refresh token for Calendar access.
     (Needs a Google Cloud "Desktop app" OAuth client: put its id/secret in
     GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET, or you'll be prompted.)
  3. Stores all four credentials as GitHub Actions *Secrets* on your repo
     via the `gh` CLI (values go over stdin -- never echoed, never committed).
  4. Asks for calendar ID + attendee emails and stores them as repo
     *Variables* (non-secret, editable anytime in the repo settings).
  5. Optionally triggers a dry-run of the import workflow.

Re-running is safe: `gh secret set` / `gh variable set` overwrite.
Nothing secret is ever printed or written to disk.
"""
import argparse
import getpass
import os
import shutil
import subprocess
import sys

SECRETS = ["GEMINI_API_KEY", "GOOGLE_CLIENT_ID",
           "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"]


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, capture_output=True, text=True,
                          **kwargs)


def have_gh():
    return shutil.which("gh") is not None


def ensure_gh_auth():
    if not have_gh():
        sys.exit("The `gh` CLI is not installed. Install it "
                 "(https://cli.github.com), then re-run this script.")
    r = subprocess.run(["gh", "auth", "status"], capture_output=True)
    if r.returncode != 0:
        print("GitHub CLI needs to log in -- a browser window will open.")
        subprocess.run(["gh", "auth", "login"], check=True)


def google_refresh_token(client_id, client_secret):
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": ["http://localhost"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        ["https://www.googleapis.com/auth/calendar"],
    )
    print("Opening your browser for Google Calendar approval...")
    creds = flow.run_local_server(port=0)
    return creds.refresh_token


def set_secret(repo, name, value):
    run(["gh", "secret", "set", name, "--repo", repo], input=value)
    print(f"  secret stored: {name}")


def set_variable(repo, name, value):
    run(["gh", "variable", "set", name, "--repo", repo, "--body", value])
    print(f"  variable stored: {name}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default="xtremejake/homelab",
                    help="Target repo as owner/name")
    ap.add_argument("--trigger-dry-run", action="store_true",
                    help="After setup, trigger a dry-run workflow run")
    ap.add_argument("--file", default="",
                    help="Newsletter filename under incoming/ (for --trigger-dry-run)")
    args = ap.parse_args()

    print("== 1/4 Gemini API key ==")
    gemini_key = getpass.getpass("GEMINI_API_KEY (https://aistudio.google.com/apikey): ").strip()
    if not gemini_key:
        sys.exit("A Gemini API key is required.")

    print("\n== 2/4 Google Calendar OAuth ==")
    client_id = os.environ.get("GOOGLE_CLIENT_ID") or \
        getpass.getpass("GOOGLE_CLIENT_ID (Desktop OAuth client): ").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or \
        getpass.getpass("GOOGLE_CLIENT_SECRET: ").strip()
    if not client_id or not client_secret:
        sys.exit("Google OAuth client id/secret are required.")
    refresh_token = google_refresh_token(client_id, client_secret)
    print("Google approval complete.")

    print(f"\n== 3/4 Storing secrets on {args.repo} ==")
    ensure_gh_auth()
    set_secret(args.repo, "GEMINI_API_KEY", gemini_key)
    set_secret(args.repo, "GOOGLE_CLIENT_ID", client_id)
    set_secret(args.repo, "GOOGLE_CLIENT_SECRET", client_secret)
    set_secret(args.repo, "GOOGLE_REFRESH_TOKEN", refresh_token)

    print("\n== 4/4 Runtime variables (non-secret) ==")
    calendar_id = input("Google Calendar ID "
                        "(e.g. foobar-family@group.calendar.google.com): ").strip()
    attendees = input("Attendee emails, comma-separated "
                      "(e.g. foobar@gmail.com): ").strip()
    if calendar_id:
        set_variable(args.repo, "CALENDAR_ID", calendar_id)
    if attendees:
        set_variable(args.repo, "ATTENDEE_EMAILS", attendees)

    print("\nSetup complete. Stored 4 secrets and "
          f"{2 if calendar_id and attendees else 1 if calendar_id or attendees else 0} "
          "variables. Values were never printed or saved locally.")

    if args.trigger_dry_run:
        if not args.file:
            sys.exit("Pass --file <name> with --trigger-dry-run.")
        print(f"\nTriggering dry-run for {args.file} ...")
        subprocess.run([
            "gh", "workflow", "run", "School calendar import",
            "--repo", args.repo,
            "-f", f"file={args.file}",
            "-f", f"calendar_id={calendar_id}",
            "-f", f"attendees={attendees}",
            "-f", "mode=dry-run",
        ], check=True)
        print("Triggered. Watch it under the repo's Actions tab.")


if __name__ == "__main__":
    main()
