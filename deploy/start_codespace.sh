#!/usr/bin/env bash
# Start the tool inside a GitHub Codespace (or any Linux machine) so you can use it in the browser.
# Usage:  bash deploy/start_codespace.sh
# Set a Codespaces secret called PDFA11Y_PASSWORD (GitHub > Settings > Codespaces > Secrets) to choose the password;
# otherwise a one-off password is generated and shown below.
cd "$(dirname "$0")/.."
PORT="${PORT:-8765}"
if [ -z "${PDFA11Y_PASSWORD:-}" ]; then
  PDFA11Y_PASSWORD="$(python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(12)))")"
  export PDFA11Y_PASSWORD
  echo "No PDFA11Y_PASSWORD secret is set, so this session uses a one-off password:"
  echo
  echo "    $PDFA11Y_PASSWORD"
  echo
fi
echo "Next: open the PORTS tab, find port $PORT, click the globe icon to open it in your browser, and sign in with the password."
echo "Only public publications, please: this is a cloud machine, not the Institute's network."
echo
exec python -m pdfa11y web --host 0.0.0.0 --port "$PORT" --data-dir "${PDFA11Y_DATA:-$HOME/pdfa11y-data}" --retention-days 7 --max-upload-mb 150
