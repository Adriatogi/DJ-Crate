# Branding

Put your logo at `branding/logo.png`: a square PNG, ideally 1024x1024 (transparent background if you want rounded corners). Then run `uv run python scripts/make_icon.py`.
It writes the app icon `djcrate/web/icon.png` (also used in the top bar, as the favicon, and for the macOS `.icns` from `scripts/make_app.sh`), plus `site/assets/logo.png` (512), `site/assets/favicon.png` (64) and `site/assets/social-preview.png` (1280x640, GitHub social preview).
With no `branding/logo.png`, the script draws the built-in vinyl record icon.
All colors for the app and the website live in `djcrate/web/theme.css`.
