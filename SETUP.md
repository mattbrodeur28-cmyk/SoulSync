# VS Code setup — SoulSync Music Lite

## 1. Copy into the repo root

```
AGENTS.md
.vscode/settings.json
.vscode/tasks.json
.vscode/extensions.json
```

## 2. .gitignore — nothing to do

Not needed after all. `.gitignore` line 42 is `**/.*/`, which already ignores
`.venv/`, `.pytest_cache/`, and `.ruff_cache/`. `gitignore-additions.txt` is
redundant and can be deleted.

Note the same pattern also ignores `.vscode/`, so the editor config in step 1
stays local unless you add a negation.

## 3. One-time environment setup

```bash
brew install python@3.11
```

Then in VS Code: **Cmd+Shift+P → Tasks: Run Task → "Setup: create 3.11 venv + install deps"**

Or by hand:

```bash
/opt/homebrew/bin/python3.11 -m venv ~/.venvs/soulsync
source ~/.venvs/soulsync/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install ruff pytest
python --version     # must print 3.11.x
```

## 4. Install the extensions — do not skip this

Without these, `settings.json` is inert and **`Python: Select Interpreter` does
not exist as a command**. This is the step whose absence broke the original
setup.

If `code` is not on your PATH, link it first (`/opt/homebrew/bin` is writable
and already on PATH, so no sudo):

```bash
ln -s "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" /opt/homebrew/bin/code
```

Then:

```bash
code --install-extension ms-python.python \
     --install-extension ms-python.vscode-pylance \
     --install-extension ms-python.debugpy \
     --install-extension charliermarsh.ruff
code --list-extensions        # confirm, then reload the window
```

`ms-python.vscode-python-envs` installs automatically as a dependency.

## 5. How you open the repo changes which settings apply

**Opening SoulSync as a single folder is the simplest option and needs nothing
extra** — every setting in `.vscode/settings.json` applies, whatever its scope.

It only gets complicated in a **multi-root** window (SoulSync alongside e.g.
Reaparr-v2). There, folder-level `.vscode/settings.json` applies only
**resource**- and **machine-overridable**-scoped settings; **window**-scoped ones
are silently ignored and must live in the `.code-workspace` file.

Scopes verified by reading the extension manifests, not assumed:

| Setting | Scope | Single folder | Multi-root |
|---|---|---|---|
| `python.defaultInterpreterPath` | machine-overridable | applies | applies |
| `python.testing.*` | resource | applies | applies |
| `ruff.interpreter` | resource | applies | applies |
| `ruff.importStrategy` | **window** | applies | **ignored** |

`ruff.importStrategy` is therefore duplicated in `.vscode/settings.json` and in
`~/SoulSync-Reaparr.code-workspace`, so the config is right either way. The
ignored copy is harmless.

If you want the multi-root window, open it from the terminal — more reliable than
the dialog, and an already-open workspace will not retarget itself:

```bash
code ~/SoulSync-Reaparr.code-workspace
```

The workspace file is kept outside both repos so neither has to track it. If you
only ever open SoulSync alone, the file is unnecessary and can be deleted.

## 6. Point VS Code at the interpreter

**Cmd+Shift+P → Python: Select Interpreter → Enter interpreter path**

```
~/.venvs/soulsync/bin/python
```

`settings.json` sets this as the default, but selecting it once makes VS Code
remember it for the workspace.

## 7. Verify

**Cmd+Shift+P → Tasks: Run Task**:

- `Lint: ruff (CI gate)` → `All checks passed!`
- `Test: plugin conformance` → `15 passed`
- `Test: torrent/usenet plugins` → `51 passed`

`Cmd+Shift+B` has no default build task here; tests are the default test group,
so **Cmd+Shift+P → Tasks: Run Test Task** runs plugin conformance.

## Shell alias

```bash
echo "alias ssvenv='source ~/.venvs/soulsync/bin/activate && cd ~/SoulSync'" >> ~/.zshrc
```

## Notes

- **Python 3.11 is required**, not optional. The Dockerfile, ruff target, and
  upstream CI all pin it. Your Homebrew default is 3.14.
- The venv lives outside the repo deliberately.
- `AGENTS.md` is read by Claude Code and most AI coding tools. If you use GitHub
  Copilot specifically, also symlink it:
  `ln -s ../AGENTS.md .github/copilot-instructions.md`
- The "FULL SUITE" task is expected to fail. It's there to measure cleanup
  progress on the ~50 broken test files.
