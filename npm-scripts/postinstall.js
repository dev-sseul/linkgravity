const { execSync } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');
const { pip: venvPip, workspaceDir } = require('./venv-paths');

console.log('⚙️  Setting up Python Virtual Environment...');
console.log(`   (in ${path.join(workspaceDir, 'venv')} - not inside this install, so it survives`);
console.log('    package updates/reinstalls and works the same whether this is a global');
console.log('    `npm install -g linkgravity` or a local dev clone.)');

// Use python on Windows, python3 on Mac/Linux - this is the *system*
// python used only to create the venv below; once it exists, every
// other script (this one included) goes through venv-paths.js instead.
const isWin = os.platform() === 'win32';
const pyCmd = isWin ? 'python' : 'python3';

async function main() {
    try {
        // 1. Create Python virtual environment (venv) in the fixed
        // workspace dir, not in cwd - see venv-paths.js for why.
        fs.mkdirSync(workspaceDir, { recursive: true });
        execSync(`${pyCmd} -m venv "${path.join(workspaceDir, 'venv')}"`, { stdio: 'inherit' });

        // 2. Install Python packages
        console.log('📦 Installing Python dependencies...');
        execSync(`"${venvPip}" install -r requirements.txt`, { stdio: 'inherit' });

        // 3. Install Node.js voice service packages (includes
        // rustpotter-web, which handles both wake-word detection AND
        // building .rpw reference files in-process - no separate
        // binary download needed for either).
        console.log('🎙️  Installing Voice Service dependencies...');
        execSync('npm install', {
            stdio: 'inherit',
            cwd: path.join(__dirname, '..', 'voice-service'),
        });

        // 4. Register this install's location with agy as its
        // tool-approval hook (~/.gemini/config/hooks.json) - always
        // re-run so the registered path self-heals if this checkout
        // gets moved/renamed later, instead of silently going stale.
        // Guarded on its own: agy may not be installed/configured yet
        // on a brand new machine, and that shouldn't fail the rest of
        // the install - just means the hook needs registering once agy
        // itself is set up (re-running `npm install` after does it).
        try {
            require('./register-hook')();
        } catch (err) {
            console.warn(
                `⚠️  Couldn't register the agy tool-approval hook: ${err.message.split('\n')[0]}`,
            );
        }

        console.log('✅ Installation complete!');
    } catch (error) {
        console.error('❌ Installation failed. Please ensure Python 3.10+ is installed.');
        process.exit(1);
    }
}

main();
