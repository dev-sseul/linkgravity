#!/usr/bin/env node

const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const {
    PLATFORMS,
    getSettings,
    platformState,
    getSessions,
    LGY_PM2_NAME,
    LGY_SCRIPT_PATH,
} = require('./platforms');

const { python: pythonExe, isWin } = require('../npm-scripts/venv-paths');

const cmd = process.argv[2];

const color = {
    reset: '\x1b[0m',
    green: '\x1b[32m',
    cyan: '\x1b[36m',
    yellow: '\x1b[33m',
    red: '\x1b[31m',
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
        // pm2 gives Python a pipe not a TTY, so it block-buffers stdout and can sit on log lines indefinitely - force line buffering.
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

// Matches a leading timestamp from either loguru or aiohttp's access-log format; only strips the first bracket group so aiohttp's second "[INFO ]" bracket is left alone.
const TIMESTAMP_PREFIX = /^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]?\s*/;
// loguru's colorize=True puts an ANSI code before the timestamp digits, breaking the '^' anchor above.
// eslint-disable-next-line no-control-regex
const ANSI_ESCAPE = /\x1b\[[0-9;]*m/g;

const LEVEL_COLOR = {
    DEBUG: color.dim,
    INFO: color.cyan,
    WARNING: color.yellow,
    ERROR: color.red,
    CRITICAL: color.red,
};

// Recolors the level word ourselves so Python (loguru) and Node (plain console.log) lines match.
function colorizeLevel(line) {
    return line.replace(/\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b/, (match) => {
        const c = LEVEL_COLOR[match];
        return c ? `${c}${match}${color.reset}` : match;
    });
}

function runPm2LogsStream(args, printLine) {
    const cp = spawn('npx', ['-y', 'pm2', ...args], { cwd: path.join(__dirname, '..') });

    const isNoise = (line) =>
        line.trim().length === 0 ||
        line.includes('In-memory PM2') ||
        line.includes('pm2 update') ||
        line.includes('[TAILING]') ||
        line.includes('.pm2/logs/lgy') ||
        line.includes('In memory PM2 version') ||
        line.includes('Local PM2 version') ||
        line.match(/^>+ /);

    const handleLine = (line) => {
        if (isNoise(line)) return;
        printLine(line);
    };

    // A line can arrive split across two 'data' events, so buffer until '\n' is seen.
    function makeChunkHandler() {
        let buffer = '';
        const handler = (data) => {
            buffer += data.toString();
            const lines = buffer.split('\n');
            buffer = lines.pop(); // last element: '' if buffer ended in '\n', else the incomplete tail
            for (const line of lines) handleLine(line);
        };
        handler.flush = () => {
            if (buffer) handleLine(buffer);
            buffer = '';
        };
        return handler;
    }

    const stdoutHandler = makeChunkHandler();
    const stderrHandler = makeChunkHandler();
    cp.stdout.on('data', stdoutHandler);
    cp.stderr.on('data', stderrHandler);
    cp.on('close', () => {
        stdoutHandler.flush();
        stderrHandler.flush();
    });
}

function runPm2LogsClean(args, showStamps = false) {
    runPm2LogsStream(args, (line) => {
        const clean = line.replace(ANSI_ESCAPE, '');
        console.log(colorizeLevel(showStamps ? clean : clean.replace(TIMESTAMP_PREFIX, '')));
    });
}

function verifyStartup() {
    return new Promise((resolve) => {
        process.stdout.write(
            `${color.cyan}▶${color.reset} Verifying startup status (waiting for bot to come online)...`,
        );

        let cp = spawn('npx', ['-y', 'pm2', 'logs', LGY_PM2_NAME, '--raw', '--lines', '20'], {
            cwd: path.join(__dirname, '..'),
        });

        let settled = false;
        const finish = (ok) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            cp.kill();
            resolve(ok);
        };

        // Give the --lines 20 replay burst a moment to flush before treating error text as a fresh crash, not old log noise.
        let errorDetectionArmed = false;
        setTimeout(() => {
            errorDetectionArmed = true;
        }, 1500);

        let timer = setTimeout(() => {
            console.log(
                `\n\n${color.yellow}⏳ Startup verification timed out. Run 'lgy logs' to check status manually.${color.reset}`,
            );
            finish(false);
        }, 30000);

        const checkLog = (data) => {
            if (settled) return;
            const str = data.toString();
            if (str.includes('Bot is fully online and ready!')) {
                console.log(`\n${color.green}✔${color.reset} Bot successfully came online!\n`);
                finish(true);
            } else if (
                errorDetectionArmed &&
                (str.includes('Traceback (most recent call last):') ||
                    str.includes('Error:') ||
                    str.includes('Exception:'))
            ) {
                console.log(`\n\n${color.yellow}❌ Error detected during startup:${color.reset}`);
                const errorLines = str
                    .split('\n')
                    .filter(
                        (l) =>
                            !l.includes('In-memory') &&
                            !l.includes('[TAILING]') &&
                            l.trim().length > 0,
                    );
                console.log(errorLines.join('\n'));
                finish(false);
            }
        };

        cp.stdout.on('data', checkLog);
        cp.stderr.on('data', checkLog);
    });
}

function formatUptime(pmUptimeMs) {
    const seconds = Math.floor((Date.now() - pmUptimeMs) / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ${minutes % 60}m`;
    const days = Math.floor(hours / 24);
    return `${days}d ${hours % 24}h`;
}

function renderTable(headers, rows) {
    const widths = headers.map((h, i) =>
        Math.max(h.length, ...rows.map((r) => String(r[i]).length)),
    );
    const pad = (s, w) => ` ${String(s).padEnd(w)} `;
    const sepLine = (l, m, r) => l + widths.map((w) => '─'.repeat(w + 2)).join(m) + r;
    const rowLine = (cells) => '│' + cells.map((c, i) => pad(c, widths[i])).join('│') + '│';

    const lines = [sepLine('┌', '┬', '┐'), rowLine(headers), sepLine('├', '┼', '┤')];
    for (const row of rows) lines.push(rowLine(row));
    lines.push(sepLine('└', '┴', '┘'));
    return lines.join('\n');
}

if (cmd === 'version' || cmd === '-v' || cmd === '--version') {
    const pkg = require('../package.json');
    console.log(`linkgravity v${pkg.version}`);
} else if (cmd === 'start') {
    info('Starting LinkGravity daemon...');
    runPm2(['start', LGY_SCRIPT_PATH, '--interpreter', pythonExe, '--name', LGY_PM2_NAME]);
    verifyStartup().then((ok) => process.exit(ok ? 0 : 1));
} else if (cmd === 'stop') {
    info('Stopping LinkGravity daemon...');
    runPm2(['stop', LGY_PM2_NAME]);
    success('Daemon stopped successfully.\n');
} else if (cmd === 'restart') {
    info('Restarting LinkGravity daemon...');
    runPm2(['restart', LGY_PM2_NAME, '--update-env']);
    verifyStartup().then((ok) => process.exit(ok ? 0 : 1));
} else if (cmd === 'logs') {
    const SHORT_FLAGS = ['-f', '-n', '-t'];
    let args = process.argv.slice(3).flatMap((arg) => {
        // Only split bare combined short flags (e.g. "-fn" -> "-f", "-n"), not "--long" flags.
        if (!/^-[a-z]{2,}$/.test(arg)) return [arg];
        const chars = arg.slice(1).split('');
        if (!chars.every((c) => SHORT_FLAGS.includes(`-${c}`))) return [arg];
        return chars.map((c) => `-${c}`);
    });
    let pm2Args = ['logs', LGY_PM2_NAME];
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
} else if (cmd === 'status') {
    const settings = getSettings();
    const sessions = getSessions();

    let pm2Procs = [];
    const jlist = spawnSync('npx', ['-y', 'pm2', 'jlist'], { stdio: 'pipe' });
    if (jlist.status === 0) {
        try {
            pm2Procs = JSON.parse(jlist.stdout.toString());
        } catch (e) {}
    }

    const proc = pm2Procs.find((p) => p.name === LGY_PM2_NAME);
    if (!proc) {
        console.log(`\n${color.cyan}▶${color.reset} daemon: not running\n`);
    } else {
        const mem = proc.monit ? `${Math.round(proc.monit.memory / 1024 / 1024)}mb` : '?';
        const cpu = proc.monit ? `${proc.monit.cpu}%` : '?';
        const uptime =
            proc.pm2_env.status === 'online' ? formatUptime(proc.pm2_env.pm_uptime) : '-';
        console.log(
            `\n${color.cyan}▶${color.reset} daemon: ${proc.pm2_env.status} (uptime: ${uptime}, restarts: ${proc.pm2_env.restart_time}, cpu: ${cpu}, mem: ${mem})\n`,
        );
    }

    const rows = Object.entries(PLATFORMS).map(([key, def]) => {
        const { enabled } = platformState(key, settings);
        const sessionCount = Object.values(sessions).filter((s) => s.platform === key).length;
        return [def.label, enabled ? 'yes' : 'no', String(sessionCount)];
    });
    console.log(renderTable(['platform', 'enabled', 'sessions'], rows));
    console.log();
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
    const restartResult = spawnSync('npx', ['-y', 'pm2', 'restart', LGY_PM2_NAME, '--update-env'], {
        stdio: 'pipe',
        cwd: path.join(__dirname, '..'),
        env: { ...process.env, PYTHONUNBUFFERED: '1' },
    });

    if (restartResult.status === 0) {
        verifyStartup().then((ok) => process.exit(ok ? 0 : 1));
    } else if ((restartResult.stderr || '').toString().includes('not found')) {
        // Wasn't running before the update - start fresh instead of a false "restarted".
        info("Daemon wasn't running - starting it fresh...");
        runPm2(['start', LGY_SCRIPT_PATH, '--interpreter', pythonExe, '--name', LGY_PM2_NAME]);
        verifyStartup().then((ok) => process.exit(ok ? 0 : 1));
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
            '  version    Print the installed version (-v, --version)',
            '  start      Start bot in the background (PM2 daemon)',
            '  stop       Stop the background bot',
            '  restart    Restart the background bot',
            '  logs       View bot logs (Options: --tail, -n, -f, -t/--timestamp)',
            '  status     Show daemon status and per-platform enabled/session counts',
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
                {
                    label: 'Status',
                    value: 'status',
                    hint: 'Show daemon status and per-platform sessions',
                },
                { label: 'Setup', value: 'setup', hint: 'Configure bot tokens and settings' },
                {
                    label: 'Update',
                    value: 'update',
                    hint: 'Check npm for a newer version and install it',
                },
                { label: 'Version', value: 'version', hint: 'Print the installed version' },
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
