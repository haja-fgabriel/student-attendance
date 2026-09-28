# Laboratory participation tracker

A small web app for maintaining a student roster and recording weekly lab participation. The weekly report prominently lists students who attended with a group other than their regular group, and can be printed or exported to CSV.

## Run locally

Requires Python 3.10 or newer; no third-party packages are needed.

```sh
python3 app.py
```

Open <http://localhost:8000>. Add groups and students under **Groups & students**, record each lab date, and generate the report for a week. Data is stored in `participation.sqlite3` next to the app. Set `DATABASE_PATH` to choose another SQLite file. Local mode binds to `127.0.0.1`.

## Deploy

The included `Procfile` and `requirements.txt` support Heroku-style Python buildpacks. Commit these files in a Git repository, then deploy:

```sh
heroku create
heroku config:set APP_PASSWORD='choose-a-long-unique-password'
git push heroku main
```

The app requires `APP_PASSWORD` whenever `PORT` is set, and protects the app with HTTP Basic authentication. SQLite is suitable for a local or single-instance self-hosted setup. Heroku's dyno filesystem is ephemeral, so the local SQLite file will not reliably persist across dyno restarts or deploys. For a Heroku deployment, use a persistent SQLite volume if the platform supports one, or migrate the database layer to a managed database before relying on the data.

## Report contents

The weekly report summarizes recorded present, absent, and excused entries, groups cross-group attendees by the group they joined, and lists daily participation by each student's regular group. Students left as “Not recorded” are excluded until attendance is saved.
