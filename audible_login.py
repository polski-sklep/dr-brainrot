"""One-time Audible sign-in for the daily Audible read. Run it yourself, in Terminal:

    cd ~/Projects/drbr && .venv/bin/python audible_login.py

It prints an Amazon sign-in link. Open it in your browser and sign in there: your
password and two-step code go to Amazon only, never to this program. When the
browser ends on a page that looks like an error, copy that page's whole address
from the address bar and paste it back here.

The sign-in is saved to data/audible_auth.json (data/ is never committed, and the
file is readable by you only). Amazon lists it as a new device under your
account's devices, where you can remove it at any time; delete the file too.
Nothing is bought, changed or read beyond one test call to your library.

Audible listening is measured but never counted as brain rot (user decision,
7 Oct 2026). The unofficial `audible` library (mkb79) does the talking; it can
stop working if Amazon changes things, and that is the only cost.
"""
from __future__ import annotations

import pathlib
import readline  # noqa: F401  -- without it, macOS Terminal cuts a pasted line off at 1,024 characters
import sys
import webbrowser

import audible

STORES = {"uk": "uk", "de": "de", "us": "us", "fr": "fr", "it": "it", "es": "es",
          "ca": "ca", "au": "au", "in": "in", "jp": "jp", "br": "br"}
AUTH_FILE = pathlib.Path(__file__).resolve().parent / "data" / "audible_auth.json"


LANDING = "openid.oa2.authorization_code="

STEPS = """
Your browser is opening Amazon's sign-in page (if it doesn't, open the link
below yourself). Sign in there; Amazon may ask twice and show a puzzle.

When you are signed in, the browser shows "Page not found". That is expected.
Copy the whole address of THAT page from the address bar. It starts with
https://www.amazon.../ap/maplanding and is very long.

Sign-in link:
{url}
"""


def _landing_address(url: str) -> str:
    """Open Amazon's sign-in page and take back the address it ends on.

    Asks again, saying why, when the pasted address isn't the one after sign-in
    (the commonest slip is pasting the sign-in link itself back).
    """
    print(STEPS.format(url=url))
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 -- the printed link is the fallback
        pass
    while True:
        pasted = input("Paste the address of the \"Page not found\" page here: ").strip()
        if LANDING in pasted:
            return pasted
        if "/ap/signin" in pasted:
            print("\nThat's the sign-in link itself. Finish signing in first, then copy the "
                  "address of the \"Page not found\" page you end on.\n")
        else:
            print("\nThat address doesn't carry the sign-in result. Copy the whole address of "
                  "the \"Page not found\" page after signing in (it contains "
                  "\"authorization_code\").\n")


def main() -> None:
    if AUTH_FILE.exists() and "--again" not in sys.argv:
        print(f"Already signed in ({AUTH_FILE}). Run with --again to sign in afresh.")
        return
    store = input("Which Audible store do you use? "
                  "(uk, de, us, fr, it, es, ca, au, in, jp, br) [uk]: ").strip().lower() or "uk"
    if store not in STORES:
        sys.exit(f"Unknown store {store!r}.")
    auth = audible.Authenticator.from_login_external(locale=STORES[store],
                                                     login_url_callback=_landing_address)
    AUTH_FILE.parent.mkdir(exist_ok=True)
    auth.to_file(AUTH_FILE, encryption=False)
    AUTH_FILE.chmod(0o600)
    with audible.Client(auth=auth) as client:
        library = client.get("1.0/library", num_results=1, response_groups="product_desc")
    count = library.get("total_results", len(library.get("items", [])))
    print(f"Signed in to Audible ({store}); your library answered ({count} titles). "
          "You can close this window.")


if __name__ == "__main__":
    main()
