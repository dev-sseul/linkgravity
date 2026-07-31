const originalLog = console.log;
const originalError = console.error;

function getTimestamp() {
    const now = new Date();
    const offset = now.getTimezoneOffset() * 60000;
    const localTime = new Date(now.getTime() - offset);
    return localTime.toISOString().replace('T', ' ').substring(0, 19);
}

console.log = function (...args) {
    originalLog(`${getTimestamp()} INFO  Voice:`, ...args);
};
console.error = function (...args) {
    originalError(`${getTimestamp()} ERROR Voice:`, ...args);
};
