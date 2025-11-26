module.exports = {
    apps: [{
        name: 'hya-ocr-backend',
        script: '/home/ubuntu/HYA-OCR/backend/.venv/bin/uvicorn',
        args: 'app:app --host 0.0.0.0 --port 8000',
        cwd: '/home/ubuntu/HYA-OCR/backend',
        interpreter: 'none',
        instances: 1,
        autorestart: true,
        watch: false,
        max_memory_restart: '1G',
        env: {
            NODE_ENV: 'production',
            PORT: '8000'
        },
        error_file: './logs/pm2-error.log',
        out_file: './logs/pm2-out.log',
        log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
        merge_logs: true,
        time: true
    }]
};
