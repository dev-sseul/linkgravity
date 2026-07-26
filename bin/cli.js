#!/usr/bin/env node

const { spawn, spawnSync } = require('child_process');
const path = require('path');

// Find the absolute path to the Python bot script
const botPath = path.join(__dirname, '..', 'src', 'main.py');
const { python: pythonExe, isWin } = require('../npm-scripts/venv-paths');

const cmd = process.argv[2];

const color = {
    reset: '\x1b[0m',
    green: '\x1b[32m',
    cyan: '\x1b[36m',
    yellow: '\x1b[33m',
    dim: '\x1b[2m',
};

function success(msg) {
    console.log(`${color.green}✔${color.reset} ${msg}`);
}

function info(msg) {
    console.log(`\n${color.cyan}▶${color.reset} ${msg}`);
}

function runPm2(args, silent = true) {
    const stdioOpt = silent ? 'pipe' : 'inherit';
    const result = spawnSync('npx', ['-y', 'pm2', ...args], {
        stdio: stdioOpt,
        cwd: path.join(__dirname, '..'),
        // pm2 pipes the Python process's stdout rather than giving it a
        // TTY, so Python defaults to block-buffering it - occasional
        // log lines (like a single WARNING) can sit in that buffer
        // indefinitely instead of reaching `pm2 logs`/bot.log. This
        // forces line-by-line flushing regardless of interpreter/OS.
        env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });

    if (result.error) {
        console.error('Failed to execute PM2:', result.error.message);
        process.exit(1);
    }

    let hasSudoInstructions = false;
    if (silent && result.stdout && (args[0] === 'startup' || args[0] === 'unstartup')) {
        const out = result.stdout.toString();
        const lines = out.split('\n');
        for (const line of lines) {
            if (
                line.trim().startsWith('sudo env PATH') ||
                line.trim().startsWith('sudo su -c') ||
                line.includes('sudo ')
            ) {
                console.log(
                    `\n\n${color.yellow}⚠ Action Required:${color.reset} To complete setup, copy and paste this command into your terminal:\n`,
                );
                console.log(`    ${color.cyan}${line.trim()}${color.reset}\n`);
                hasSudoInstructions = true;
            }
        }
    }

    if (result.status !== 0 && !hasSudoInstructions) {
        if (silent && result.stderr) {
            console.error(result.stderr.toString().trim());
        }
        process.exit(result.status);
    }
}

// Matches a leading timestamp in either format our logs actually use:
//   "2026-07-19 19:11:25 INFO  ..."          (loguru)
//   "[2026-07-19 14:31:53] [INFO    ] ..."   (aiohttp access log)
// Only strips the FIRST bracket group if present, so aiohttp's second
// "[INFO ]" bracket (not a timestamp) is left alone.
const TIMESTAMP_PREFIX = /^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]?\s*/;

function runPm2LogsClean(args, showStamps = false) {
    const cp = spawn('npx', ['-y', 'pm2', ...args], { cwd: path.join(__dirname, '..') });

    const filterAndPrint = (data) => {
        const lines = data.toString().split('\n');
        for (const line of lines) {
            if (line.trim().length === 0) continue;
            if (
                line.includes('In-memory PM2') ||
                line.includes('pm2 update') ||
                line.includes('[TAILING]') ||
                line.includes('.pm2/logs/lgy') ||
                line.includes('In memory PM2 version') ||
                line.includes('Local PM2 version') ||
                line.match(/^>+ /)
            ) {
                continue;
            }
            console.log(showStamps ? line : line.replace(TIMESTAMP_PREFIX, ''));
        }
    };

    cp.stdout.on('data', filterAndPrint);
    cp.stderr.on('data', filterAndPrint);
}

function verifyStartup() {
    process.stdout.write(
        `${color.cyan}▶${color.reset} Verifying startup status (waiting for bot to come online)...`,
    );

    let cp = spawn('npx', ['-y', 'pm2', 'logs', 'lgy', '--raw', '--lines', '0'], {
        cwd: path.join(__dirname, '..'),
    });

    let timer = setTimeout(() => {
        console.log(
            `\n\n${color.yellow}⏳ Startup verification timed out. Run 'lgy logs' to check status manually.${color.reset}`,
        );
        cp.kill();
        process.exit(1);
    }, 15000);

    const checkLog = (data) => {
        const str = data.toString();
        if (str.includes('Bot is fully online and ready!')) {
            clearTimeout(timer);
            console.log(
                `\n${color.green}✔${color.reset} Bot successfully came online and is connected to Discord!\n`,
            );
            cp.kill();
            process.exit(0);
        } else if (
            str.includes('Traceback (most recent call last):') ||
            str.includes('Error:') ||
            str.includes('Exception:')
        ) {
            clearTimeout(timer);
            console.log(`\n\n${color.yellow}❌ Error detected during startup:${color.reset}`);
            const errorLines = str
                .split('\n')
                .filter(
                    (l) =>
                        !l.includes('In-memory') && !l.includes('[TAILING]') && l.trim().length > 0,
                );
            console.log(errorLines.join('\n'));
            cp.kill();
            process.exit(1);
        }
    };

    cp.stdout.on('data', checkLog);
    cp.stderr.on('data', checkLog);
}

if (cmd === 'start') {
    info('Starting LinkGravity daemon...');
    runPm2(['start', botPath, '--interpreter', pythonExe, '--name', 'lgy']);
    verifyStartup();
} else if (cmd === 'stop') {
    info('Stopping LinkGravity daemon...');
    runPm2(['stop', 'lgy']);
    success('Daemon stopped successfully.\n');
} else if (cmd === 'restart') {
    info('Restarting LinkGravity daemon...');
    runPm2(['restart', 'lgy', '--update-env']);
    verifyStartup();
} else if (cmd === 'logs') {
    let args = process.argv.slice(3);
    let pm2Args = ['logs', 'lgy'];
    let isFollow = false;
    let showStamps = false;

    for (let i = 0; i < args.length; i++) {
        if (args[i] === '--tail' || args[i] === '-n') {
            pm2Args.push('--lines', args[i + 1] || '15');
            i++;
        } else if (args[i] === '-f') {
            isFollow = true;
        } else if (
            args[i] === '-t' ||
            args[i] === '--stamp' ||
            args[i] === '--timestamp' ||
            args[i] === '--timestamps'
        ) {
            showStamps = true;
        } else {
            pm2Args.push(args[i]);
        }
    }

    if (!isFollow) {
        pm2Args.push('--nostream');
    }
    pm2Args.push('--raw');
    runPm2LogsClean(pm2Args, showStamps);
} else if (cmd === 'enable') {
    if (isWin) {
        console.log(
            `\n${color.yellow}⚠${color.reset} pm2 doesn't support auto-start-on-boot on Windows natively. ` +
                `Use a third-party tool like pm2-windows-startup (https://github.com/marklagendijk/node-pm2-windows-startup) instead.`,
        );
        process.exit(1);
    }
    info('Registering LinkGravity to start on system boot...');
    runPm2(['startup']);
    runPm2(['save']);
    success('Auto-start configuration saved.\n');
} else if (cmd === 'disable') {
    if (isWin) {
        console.log(
            `\n${color.yellow}⚠${color.reset} pm2 doesn't support auto-start-on-boot on Windows natively - ` +
                `nothing to disable here. If you set it up via a third-party tool, remove it through that tool.`,
        );
        process.exit(1);
    }
    info('Removing LinkGravity from system boot...');
    runPm2(['unstartup']);
    runPm2(['save']);
    success('Auto-start configuration removed.\n');
} else if (cmd === 'setup' || cmd === 'init') {
    const runSetup = require('./setup');
    runSetup().catch((err) => {
        console.error('Setup wizard crashed:', err.message);
    });
} else if (cmd === 'update') {
    const pkg = require('../package.json');
    const currentVersion = pkg.version;

    info('Checking npm for the latest version...');
    const viewResult = spawnSync('npm', ['view', 'linkgravity', 'version'], { stdio: 'pipe' });
    if (viewResult.error || viewResult.status !== 0) {
        console.error(
            (viewResult.stderr || '').toString().trim() ||
                'Failed to check the latest version on npm.',
        );
        process.exit(1);
    }
    const latestVersion = viewResult.stdout.toString().trim();

    if (latestVersion === currentVersion) {
        success(`Already up to date (v${currentVersion}).\n`);
        process.exit(0);
    }

    info(`Updating: v${currentVersion} -> v${latestVersion}...`);
    const installResult = spawnSync('npm', ['install', '-g', 'linkgravity@latest'], {
        stdio: 'inherit',
    });
    if (installResult.status !== 0) {
        console.error('npm install failed - update aborted, still on the old version.');
        process.exit(1);
    }
    success(`Installed v${latestVersion}.`);

    info('Restarting daemon to apply the update...');
    const restartResult = spawnSync('npx', ['-y', 'pm2', 'restart', 'lgy', '--update-env'], {
        stdio: 'pipe',
        cwd: path.join(__dirname, '..'),
        env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });

    if (restartResult.status === 0) {
        verifyStartup();
    } else if ((restartResult.stderr || '').toString().includes('not found')) {
        // Daemon wasn't running before the update - start it fresh instead
        // of reporting a restart that never had anything to restart.
        info("Daemon wasn't running - starting it fresh...");
        runPm2(['start', botPath, '--interpreter', pythonExe, '--name', 'lgy']);
        verifyStartup();
    } else {
        console.error((restartResult.stderr || '').toString().trim());
        console.error(
            `\n${color.yellow}⚠${color.reset} Update installed, but restarting the daemon failed - run 'lgy restart' manually.`,
        );
        process.exit(1);
    }
} else if (cmd === 'help') {
    console.log(
        [
            '',
            '🌌 LinkGravity (lgy / linkgravity)',
            '',
            'Usage: lgy <command> [options]',
            '',
            'Commands:',
            '  start      Start bot in the background (PM2 daemon)',
            '  stop       Stop the background bot',
            '  restart    Restart the background bot',
            '  logs       View bot logs (Options: --tail, -n, -f, -t/--timestamp)',
            '  enable     Register bot to start automatically on system boot',
            '  disable    Remove bot from system boot',
            '  setup      Run the configuration wizard (init)',
            '  update     Check npm for a newer version and install + restart if found',
            '  help       Show this help message',
            '',
        ].join('\n'),
    );
} else if (!cmd) {
    const p = require('@clack/prompts');
    (async () => {
        console.log();
        p.intro(`${color.cyan}🌌 LinkGravity Interactive Menu${color.reset}`);

        const action = await p.select({
            message: 'What would you like to do?',
            options: [
                { label: 'Start', value: 'start', hint: 'Start the bot daemon in the background' },
                { label: 'Stop', value: 'stop', hint: 'Stop the running daemon' },
                { label: 'Restart', value: 'restart', hint: 'Restart the running daemon' },
                { label: 'Logs', value: 'logs', hint: 'View the live console logs' },
                { label: 'Setup', value: 'setup', hint: 'Configure bot tokens and settings' },
                {
                    label: 'Update',
                    value: 'update',
                    hint: 'Check npm for a newer version and install it',
                },
                {
                    label: 'Enable Auto-start',
                    value: 'enable',
                    hint: 'Turn ON automatic boot on system startup',
                },
                { label: 'Disable Auto-start', value: 'disable', hint: 'Turn OFF automatic boot' },
                { label: 'Exit', value: 'exit', hint: 'Close this menu' },
            ],
        });

        if (p.isCancel(action) || action === 'exit') {
            p.cancel('Menu closed.');
            process.exit(0);
        }

        p.outro(`Executing: ${action}`);
        const { spawnSync } = require('child_process');
        spawnSync(process.argv[0], [process.argv[1], action], { stdio: 'inherit' });
    })();
} else {
    // If no valid command was provided, show help
    console.log(
        `\n❌ Unknown command: ${cmd || 'none'}\n💡 Run 'lgy help' to see available commands.`,
    );
}
