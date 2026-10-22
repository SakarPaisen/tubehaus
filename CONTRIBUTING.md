# Contributing

Thanks for helping out! Bug reports, translations and pull requests are all welcome.

## Reporting a bug

Open an [issue](https://github.com/biolend83/tubehaus/issues/new/choose) and include your setup
(docker compose or manual), device and browser, and the logs (`docker compose logs tubehaus --tail 100`).
If plain Invidious fails the same way, the problem is probably upstream in Invidious or its companion.

## Pull requests

1. Fork the repo and create a branch
2. Make your change. For anything big, open an issue first so we can agree on the approach
3. Open a pull request describing what changed and how you tested it

The code is deliberately dependency-free:

- `web/` is plain ES modules, no build step. Edit and reload
- `server/tubehaus_server.py` uses only the Python standard library
- Shaka Player in `web/y/lib/` is vendored; don't edit it

Before you push, the same checks CI runs:

```sh
for f in web/y/*.js web/y/lang/*.js; do cp "$f" /tmp/c.mjs && node --check /tmp/c.mjs || echo "FAIL $f"; done
python3 -m py_compile server/tubehaus_server.py
```

## Translations

Interface text lives in `web/y/lang/<code>.js`, keyed by the original Korean text. To add a language,
copy `en.js`, translate the values, and add the code to `LANGS` in `web/y/i18n.js`. Fixes to existing
translations are very welcome; several were machine-assisted.

## License

By contributing you agree that your work is released under the [AGPL-3.0](LICENSE), like the rest of the project.
