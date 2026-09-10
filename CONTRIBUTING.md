# Contributing

Bug reports and focused pull requests are welcome. Search existing issues
first and keep one problem per issue.

For a bug, include the Synapse version, `herdr --version`, operating system,
the shortest reproduction, expected behavior, and the smallest useful log
excerpt. Remove board contents, paths, tokens, credentials, and provider
responses. Report security issues privately as described in
[SECURITY.md](SECURITY.md).

Before opening a pull request, run:

```bash
python3 -m unittest discover -s tests
python3 -m herdr_team.reference_docs --check
sh -n bin/herdr-synapse bin/hook bin/herdr-synapse-sandbox console.sh
```

Update the relevant documentation with user-visible behavior. Keep changes
small and do not add dependencies when the Python standard library is enough.
