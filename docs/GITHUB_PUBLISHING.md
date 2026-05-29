# GitHub Publishing Checklist

This project was prepared for publishing without user-specific files.

Recommended repository name on GitHub: `neon-rift`

## 1) Initialize Repository

```powershell
git init
git branch -M main
```

## 2) Verify Ignore Rules

Check what will be committed:

```powershell
git status --short
```

Expected ignored local content includes:

- `.venv/`
- `.neon_rift.config.json`
- `music/in_game/`
- `graphify-out/`
- `raw/`
- local logs

## 3) First Commit

```powershell
git add .
git commit -m "Initial project import"
```

If commit fails due to missing identity:

```powershell
git config user.name "Your Name"
git config user.email "you@example.com"
```

## 4) Connect GitHub Remote

Create an empty GitHub repository, then:

```powershell
git remote add origin https://github.com/<owner>/neon-rift.git
git push -u origin main
```

## Notes

- Music assets are intentionally not committed.
- Use `ingest_music_catalog.py` to build local catalog/media after cloning.
- Keep `.neon_rift.config.json` local; only `.neon_rift.config.example.json` is tracked.
