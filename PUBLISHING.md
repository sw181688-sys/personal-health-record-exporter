# Publishing the public documentation page

Epic's registration form has a **Public Documentation URL** field. This is how
you get a real URL to put in it.

Total time: about five minutes, most of it waiting for GitHub Pages to build.

---

## 1. Create an empty repository on GitHub

Go to <https://github.com/new>:

- **Repository name:** `personal-health-record-exporter`
- **Visibility:** **Public** — this is required. Epic's documentation URL and
  the patient consent screen both need a page anyone can load without signing in.
- **Do not** check "Add a README", "Add .gitignore", or "Choose a license" —
  this project already has all three, and pre-adding them creates a conflict on
  your first push.

Click **Create repository**, then leave that page open — you'll need the URL.

---

## 2. Fill in your username

The docs contain a `sw181688-sys` placeholder in a few places. Replace
it in one shot from inside the project folder:

```bash
./set-username.sh your-github-username
```

Or do it by hand — it appears in `README.md`, `index.html`, and `SETUP.md`.

---

## 3. Push

```bash
git init -b main
git add .
git commit -m "Personal Health Record Exporter"
git remote add origin https://github.com/sw181688-sys/personal-health-record-exporter.git
git push -u origin main
```

> **Before you commit, sanity-check that no health data is going up.**
> `git status --short` should list only source and documentation files. If you
> have already done a test run and see `record/` or `sandbox/` in that list,
> stop — the `.gitignore` isn't being applied. Fix that before pushing; a
> pushed public commit containing your chart is very hard to fully undo.

---

## 4. Turn on GitHub Pages

In the new repository: **Settings → Pages**.

- **Source:** Deploy from a branch
- **Branch:** `main`, folder `/ (root)`
- **Save**

Give it one to two minutes. The page will be live at:

```
https://sw181688-sys.github.io/personal-health-record-exporter/
```

---

## 5. Verify before you paste it into Epic

Open that URL **in a private/incognito window**. This matters — a signed-in
browser can load pages an anonymous visitor can't, and Epic's reviewer and your
future consent screen are both anonymous visitors.

You should see the styled project page, with your username in the source-code
link rather than the placeholder.

---

## 6. Fill in the Epic form

| Field | Value |
|---|---|
| Automatic Client Distribution | **USCDI v3** |
| Public Documentation URL | `https://` + `sw181688-sys.github.io/personal-health-record-exporter/` |

Set the dropdown to **https://** and paste the rest of the URL without the
scheme, since the dropdown supplies it.

---

## Notes

- **Making the repo public exposes the code, not your data.** The code is
  already written to be read — that's the point of a documentation URL. Your
  record never enters the repository; `.gitignore` excludes the output folders
  and the token file.
- **If you'd rather not publish the code**, host `index.html` alone anywhere
  that serves static files (Netlify, Cloudflare Pages, your own domain) and use
  that URL instead. Epic asks for public *documentation*, not public source.
- **Keep the page reachable.** If the URL later 404s, you've broken the
  documentation link on your own app's consent screen.
