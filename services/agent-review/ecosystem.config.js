module.exports = {
    apps: [
        {
            name:          'agent-review',
            script:        'server.js',
            cwd:           __dirname,
            restart_delay: 3000,
        },
    ],
};
