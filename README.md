# Apps pipeline

`app.py` builds signed release `.apk` / `.aab` files for the apps in `all-app.json`, and can upload
an `.aab` to Google Play Console as a new version of an **existing** app. Most apps are Expo apps; plain
Kotlin/Gradle apps (like `scroll-guard`) are marked `"type": "native"` and are built straight from their
`gradlew`, with the version read from and bumped in `app/build.gradle.kts`.

```bash
python app.py                     # interactive menu
python app.py upload battleship   # upload BATTLESHIP_AAB_PATH to Google Play (asks for the track)
python app.py upload battleship --track internal --notes "Bug fixes"
```

## Uploading to Google Play Console

The upload works like this:

1. It checks the service account key, the `.aab` file, the package name, and the upload-key signature.
2. It asks you which track to use and for optional release notes.
3. It uploads the `.aab` and **publishes** it on that track (100% rollout). This is the same as
   *Save and publish* in Play Console.

What happens after publishing depends on the track and the app:

- `internal`: testers get the update within a few minutes.
- Other tracks: the release is sent to Google for review and goes live once the review passes.
- If **managed publishing** is on, or a previous update was rejected, Play won't send the release for review
  automatically. Send it yourself from *Publishing overview* in Play Console.
- If the app has never had a published release, Play only accepts drafts. The script saves a draft and tells you
  to publish it in Play Console.

At the end, the script prints either `UPLOADED TO GOOGLE PLAY`, with the status and the next step, or
`UPLOAD FAILED while <step>`, with the reason and, when it can, how to fix it.

You can start an upload in three ways:

- Menu option **11**. It uploads the file in `<PREFIX>_AAB_PATH` from `.env`.
- `python app.py upload <app> [--track internal|alpha|beta|production] [--notes "..."]`.
- After a successful `.aab` build (menu option 5 / `python app.py aab <app>`), the script asks
  "Upload it to Google Play Console now?" If you say yes, it uploads the file that was just built.

| Track        | In Play Console     |
| ------------ | ------------------- |
| `internal`   | Internal testing    |
| `alpha`      | Closed testing      |
| `beta`       | Open testing        |
| `production` | Production          |

### Limits

- The app must already exist in Play Console, and its first release must have been uploaded by hand.
  The API cannot create new apps.
- Every upload needs a `versionCode` higher than any `versionCode` Play has seen before. Menu option 5 increases
  it automatically.
- The `.aab` must be signed with the app's **upload key**, the one shown in Play Console under
  *Test and release > App integrity*.

## One-time setup: get a Google Play Console API key

Google Play does not use a simple API key. It uses a **service account**, a robot Google account,
with a **JSON key file**. You create it once, and it can upload every app in your developer account.

### 1. Create (or pick) a Google Cloud project

1. Open <https://console.cloud.google.com/>, signed in with the Google account that owns or administers
   your Play developer account.
2. In the project picker at the top, click **New project** and give it a name, for example `play-uploads`.
   You can also reuse an existing project.

### 2. Enable the Google Play Android Developer API

1. With that project selected, open
   <https://console.cloud.google.com/apis/library/androidpublisher.googleapis.com>.
2. Click **Enable**.

### 3. Create the service account and download its JSON key

1. Go to **IAM & Admin > Service accounts**: <https://console.cloud.google.com/iam-admin/serviceaccounts>.
2. Click **Create service account**, give it a name (for example `play-uploader`), and click **Create and continue**.
   It does not need any Cloud roles, so click **Done**.
3. Click the new service account, open the **Keys** tab, and choose **Add key > Create new key > JSON > Create**.
   A `.json` file is downloaded. **Keep it secret.** Anyone who has it can publish to your apps.
4. Move the file somewhere outside any git repo, for example
   `C:/Users/<you>/.android-keys/play-service-account.json`.
5. Copy the service account's email address (`play-uploader@<project>.iam.gserviceaccount.com`).
   You need it in step 4.

> If key creation is blocked with "Service account key creation is disabled", an organization policy
> (`iam.disableServiceAccountKeyCreation`) is turned on. An organization admin must allow keys for this project.

### 4. Give the service account access in Play Console

1. Open <https://play.google.com/console> and go to **Users and permissions**.
2. Click **Invite new users**, then paste the service account email.
3. Under **App permissions**, add the apps it should upload. Under **Account permissions**, you can also grant
   access to all apps. Give it at least these permissions:
   - **View app information and download bulk reports (read-only)**
   - **Release apps to testing tracks**
   - **Release to production, exclude devices, and use Play App Signing**. This is only needed if you upload to `production`.4. Click **Invite user**, then **Send invite**. Service accounts are accepted automatically.

> New permissions can take **up to 24 hours** to start working. Until then, uploads fail with
> "The caller does not have permission".

> Older Play Console accounts may also have a **Setup > API access** page. If yours does, link the Cloud
> project from step 1 there too.

### 5. Add the values to `.env`

Copy the new variables from `.env.example` into `.env`, then fill them in:

```dotenv
# One key for all apps
PLAY_SERVICE_ACCOUNT_JSON=C:/Users/<you>/.android-keys/play-service-account.json

# Per app: the .aab to upload (required for menu option 11 / `python app.py upload <app>`)
BATTLESHIP_AAB_PATH=C:/all-projects/battleship/release/app-release.aab
BATTLESHIP_PACKAGE_NAME=com.nightwise.battleship
```

The variable names for each app are listed under `env` in `all-app.json`
(`aab_path`, `package_name`, ...). Use absolute paths with forward slashes.

### 6. Install the Python libraries

```bash
pip install -r requirements.txt
```

### 7. Try it

```bash
python app.py upload battleship --track internal
```

## Troubleshooting

| Error contains                                     | Fix                                                                                     |
| -------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `has not been used in project ... or it is disabled` | Step 2: enable the API in the Cloud project that owns the service account.            |
| `The caller does not have permission`              | Step 4: invite the service account to this app, then wait up to 24 hours.               |
| `Package not found`                                | `<PREFIX>_PACKAGE_NAME` is wrong, or the app is in a different developer account.       |
| `version code ... has already been used`           | Build a new `.aab` with a higher `versionCode` (menu option 5).                         |
| `signed with the wrong key`                        | Sign with the upload key shown in Play Console > App integrity.                         |
| `invalid_grant` / `Invalid JWT`                    | The key was deleted, or this PC's clock is wrong. Create a new key (step 3).            |
| `Google API libraries are not installed`           | `pip install -r requirements.txt`                                                       |
