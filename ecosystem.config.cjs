module.exports = {
  apps: [
    {
      name: "markinote",
      cwd: "/www/wwwroot/MarkiNote",
      script: ".venv/bin/gunicorn",
      args: "--bind 127.0.0.1:3101 --workers 2 --threads 4 --timeout 120 --access-logfile - --error-logfile - main:app",
      interpreter: "none",
      instances: 1,
      exec_mode: "fork",
      watch: false,
      autorestart: true,
      max_memory_restart: "512M",
      env: {
        PYTHONUNBUFFERED: "1",
        FLASK_ENV: "production"
      }
    }
  ]
};
