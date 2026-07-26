'use strict';
// Single source of truth for "where's the venv's stuff on this OS".
// Previously bin/cli.js, postinstall.js, prepare.js, and
// .lintstagedrc.cjs each computed their own `isWin ? 'Scripts' : 'bin'`
// branch independently - four copies of the same three lines, with no
// guarantee they'd stay in sync if one changed. Everything that needs
// a path inside venv/ should require() this instead of recomputing it.
const path = require('path');
const os = require('os');

const repoRoot = path.join(__dirname, '..');
const isWin = os.platform() === 'win32';

// The venv is generated, mutable, potentially large (hundreds of MB
// once dependencies are installed) runtime state - not "package code" -
// so, like every other piece of this project's persistent state
// (lgy.json, logs/, wake_refs/ - see WORKSPACE_DIR in src/config.py),
// it belongs in the user's own fixed data directory, not wherever this
// copy of the code happens to be checked out or installed.
//
// This matters concretely for a real `npm install -g linkgravity`: the global
// npm install location often isn't writable without sudo, and isn't
// meant to hold generated mutable data in the first place - npm can
// replace that directory's contents wholesale on update/reinstall.
// Putting the venv there would reproduce both problems. One fixed
// location, shared by every checkout/install on the machine, matches
// this project's existing single-workspace assumption (there's already
// only one lgy.json / one wake_refs/ / one logs/ for the whole
// machine, not one per checkout).
const workspaceDir = path.join(os.homedir(), '.gemini', 'linkgravity');
const venvBinDir = path.join(workspaceDir, 'venv', isWin ? 'Scripts' : 'bin');

function venvBin(name) {
    return path.join(venvBinDir, isWin ? `${name}.exe` : name);
}

module.exports = {
    repoRoot, // where the CODE lives (this checkout/install) - hooks/hook.py, src/main.py, etc.
    workspaceDir, // where generated/user DATA lives (venv, logs, lgy.json, wake_refs, ...)
    isWin,
    venvBinDir,
    python: venvBin('python'),
    pip: venvBin('pip'),
    preCommit: venvBin('pre-commit'),
};
