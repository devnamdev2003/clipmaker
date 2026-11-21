# Instagram Reels upload - setup (one time)

Install and setup for `instagram_uploader.py`, the same way YouTube has its own setup.
Skip this if you only want YouTube.

> **Reels are public as soon as they are posted.** Instagram has no "private" or "unlisted" mode for API uploads.

## 1. Install the package

```bash
pip install -r requirements.txt
```

(or only: `pip install requests`)

## 2. Prepare your Instagram account

1. In the Instagram app: **Settings -> Account type and tools -> Switch to professional account** (Business or Creator).
2. Link it to a **Facebook Page** (Instagram app: *Settings -> Account Center*, or on Facebook: *Page settings -> Linked accounts*).

## 3. Create a Meta app

1. Open <https://developers.facebook.com/apps/> -> **Create app** -> type **Business**.
2. Add the product **Instagram** (the "API setup with Facebook login" option).
3. If the app is in Development mode, that is fine for your own account: add yourself under **App roles** and use your own account only.

## 4. Get an access token

1. Open **Tools -> Graph API Explorer**, choose your app.
2. Add these permissions: `instagram_basic`, `instagram_content_publish`, `pages_show_list`, `pages_read_engagement`.
3. Click **Generate Access Token** and log in with the Facebook account that manages the Page.
4. Make it long-lived (about 60 days) by opening this URL in your browser (fill in the four values):

```
https://graph.facebook.com/v23.0/oauth/access_token?grant_type=fb_exchange_token&client_id=APP_ID&client_secret=APP_SECRET&fb_exchange_token=SHORT_TOKEN
```

   `APP_ID` and `APP_SECRET` are under **App settings -> Basic**. Copy `access_token` from the answer.

## 5. Find your Instagram user id

In Graph API Explorer (with your token) run:

```
me/accounts?fields=name,instagram_business_account
```

Copy the number inside `instagram_business_account -> id`. That is `INSTAGRAM_USER_ID` (not your @username).

## 6. Put them in `.env`

```env
INSTAGRAM_ACCESS_TOKEN=your_long_lived_token
INSTAGRAM_USER_ID=17841xxxxxxxxxxxxx
```

Optional settings are in `.env.example` (`INSTAGRAM_SHARE_TO_FEED`, `POST_DESTINATION`).

## 7. Test it

Open the **Instagram** tab in the app and press **Test connection**, or in a terminal:

```bash
python instagram_uploader.py --check
```

You should see `Connected as @your_username`.

## Using it

- In the app, finished clips are listed in the **Instagram** tab (next to Upload). Check the captions, untick clips you do not want, press **Upload to Instagram**.
- Above the tabs, choose **Post clips to: YouTube / Instagram / Both** and press **Upload clips now**. Your choice is remembered. "Both" uploads to YouTube and Instagram at the same time.
- From a terminal:

```bash
python instagram_uploader.py "output/<Video Title>"      # every .mp4 in the folder
python instagram_uploader.py clip1.mp4 clip2.mp4 --yes   # specific files, no question
python instagram_uploader.py --check                     # only test the login
```

Already-posted clips are remembered in `output/<Video Title>/instagram_uploads.json` and never posted twice.

## Good to know

- Captions come from the **Instagram caption** written by Gemini (saved next to each clip). Max 2200 characters and 30 hashtags; the app trims extra automatically.
- Use **9:16 vertical** clips (the app's default). Reels can be up to a few minutes long; very long clips may be refused by Instagram.
- Instagram allows about **100 API posts per day**.
- **Token expired** (error 190 / "invalid or expired"): repeat step 4 and update `INSTAGRAM_ACCESS_TOKEN` in `.env`. No restart needed: press Test connection.
- **Permission error** (code 10 / 200): the token is missing `instagram_content_publish`; generate it again with all permissions from step 4.
- **"Instagram could not process the video"**: re-render the clip (H.264 video + AAC audio, 9:16) and try again.
- If Meta retires the API version, set `INSTAGRAM_API_VERSION` in `.env` to a newer one.
- Never share `.env` (it holds your token). It is already in `.gitignore`.
