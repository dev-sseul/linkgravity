require('./logger');

const { Client, GatewayIntentBits, Events } = require('discord.js');
const express = require('express');

const { aglConfig } = require('./config');
const state = require('./state');

process.on('unhandledRejection', (reason) => {
    console.error('Unhandled promise rejection (voice service stays alive):', reason);
});

process.on('uncaughtException', (err) => {
    // Logs the cause before exiting - an unhandled sync error used to kill the process with no trace.
    console.error('Uncaught exception - voice service is exiting:', err);
    process.exit(1);
});

const { registerRoutes } = require('./routes');

if (aglConfig.voice_threshold) {
    state.runtime.vadThreshold = parseInt(aglConfig.voice_threshold) || 3000;
}

const app = express();
app.use(express.json({ limit: '50mb' }));

const client = new Client({
    intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates],
});

client.once(Events.ClientReady, () => {
    console.log(`🎤 Node.js Voice Microservice is online as ${client.user.tag}`);
});

registerRoutes(app, client);

// Without this, SIGTERM (lgy stop/restart) kills the process mid-connection and Discord never gets a clean leave.
function shutdownGracefully() {
    console.log(
        `[Shutdown] Disconnecting from ${state.connections.size} active voice connection(s)...`,
    );
    for (const connection of state.connections.values()) {
        try {
            connection.destroy();
        } catch (e) {
            // already destroyed/disconnected - fine
        }
    }
    process.exit(0);
}

process.on('SIGTERM', shutdownGracefully);
process.on('SIGINT', shutdownGracefully);

const PORT = 18081;
app.listen(PORT, '0.0.0.0', () => {
    console.log(`Node.js Voice API listening on port ${PORT}`);
    client.login(process.env.DISCORD_TOKEN).catch((err) => {
        console.error('Failed to log in to Discord:', err.message);
        console.error(
            'Voice features will be unavailable until this is fixed - check your Discord token (run `lgy setup`).',
        );
    });
});
