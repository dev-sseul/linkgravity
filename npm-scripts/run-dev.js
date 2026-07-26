#!/usr/bin/env node
// Used by the "start"/"dev" npm scripts.
'use strict';
const { spawnSync } = require('child_process');
const path = require('path');
const { python, repoRoot } = require('./venv-paths');

const result = spawnSync(python, [path.join(repoRoot, 'src', 'main.py')], { stdio: 'inherit' });
process.exit(result.status === null ? 1 : result.status);
