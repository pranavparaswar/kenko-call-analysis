#!/bin/bash
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Workaround: this image's system Pillow (/usr/lib/python3/dist-packages) ships
# compiled extensions built for a different Python ABI than the python3 this
# repo's scripts run under, so `from PIL import Image` fails with
# "cannot import name '_imaging'". pip sees *a* Pillow already "installed" and
# won't touch it on a plain `pip install -r requirements.txt`, so force a
# reinstall of a matching build every session start.
python3 -m pip install --force-reinstall pillow
