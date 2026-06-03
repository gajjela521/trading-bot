"""
One-time setup script — run this ONCE locally to create your Gist.
After running, copy the printed GIST_ID into your GitHub Secrets.

Usage:
  python setup_gist.py YOUR_GITHUB_TOKEN
"""
import sys
import json
import urllib.request

def main():
    if len(sys.argv) < 2:
        print("Usage: python setup_gist.py YOUR_GITHUB_TOKEN")
        sys.exit(1)

    token = sys.argv[1].strip()
    payload = json.dumps({
        "description": "Trading bot — persistent trade records (do not delete)",
        "public": False,
        "files": {
            "trade_records.json": {"content": "[]"}
        }
    }).encode()

    req = urllib.request.Request(
        "https://api.github.com/gists",
        data=payload,
        headers={
            "Authorization": f"token {token}",
            "Accept":        "application/vnd.github.v3+json",
            "Content-Type":  "application/json",
            "User-Agent":    "trading-bot-setup",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        gist_id  = data["id"]
        gist_url = data["html_url"]
        print("\n✓ Gist created successfully!\n")
        print(f"  Gist URL : {gist_url}")
        print(f"  Gist ID  : {gist_id}")
        print("\nNext steps:")
        print("  1. Go to: your repo → Settings → Secrets → Actions")
        print(f"  2. Add secret  GIST_ID    = {gist_id}")
        print(f"  3. Add secret  GIST_TOKEN = {token}")
        print("     (use a token with 'gist' scope — can reuse your existing token)")
        print("\nDone! Your bot will now persist all trade records to this Gist.")
    except Exception as exc:
        print(f"Error creating Gist: {exc}")
        sys.exit(1)

if __name__ == "__main__":
    main()
