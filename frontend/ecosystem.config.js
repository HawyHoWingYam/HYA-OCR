module.exports = {
    apps: [{
        name: 'hya-ocr-frontend',
        script: 'npm',
        args: 'start',
        cwd: '/home/ubuntu/HYA-OCR/frontend',
        instances: 1,
        autorestart: true,
        watch: false,
        max_memory_restart: '500M',
        env: {
            NODE_ENV: 'production',
            PORT: '3000'
        },
        error_file: './logs/pm2-error.log',
        out_file: './logs/pm2-out.log',
        log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
        merge_logs: true,
        time: true
    }]
};
