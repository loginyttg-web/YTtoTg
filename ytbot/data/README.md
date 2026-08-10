# Cookie folder — `cookies.txt` yahan rakhein

**Local/default setup:** exported YouTube cookie file ko isi folder mein is
exact naam se rakhein:

```text
ytbot/data/cookies.txt
```

Is repo ko run karte waqt bot `ytbot/` folder se start hota hai, isliye bot ke
andar yeh path `data/cookies.txt` hota hai. Blank/example cookie file mat
banaiye — apne logged-in browser se export ki hui asli file hi paste/copy karein.

## Recommended Telegram method

1. Bot chat mein `/cookies` bhejein.
2. `cookies.txt` ko **Document/File** ke roop mein send karein — prompt ko reply
   kar sakte hain, ya command ke baad same chat mein 15 minutes ke andar direct
   file send kar sakte hain.
3. Bot `✅ Cookies Loaded` confirmation bhejega. Phir `/authstatus` chalayein.

The file must be a **Netscape-format** cookie export, normally beginning with:

```text
# Netscape HTTP Cookie File
```

Use the **Get cookies.txt LOCALLY** browser extension while logged into
YouTube. A Chrome JSON export, screenshot, pasted text, or renamed HTML page
will be rejected so it cannot replace working cookies.

## Railway / custom storage

If your environment has `DATA_DIR=/data/ytdata` (the Railway recommendation),
the manual path is instead:

```text
/data/ytdata/cookies.txt
```

If `COOKIES_PATH` is set, that explicit full file path is used instead.

> **Security:** This folder's runtime files, including `cookies.txt`, are
> git-ignored. Never commit, share, or upload your cookie file to GitHub.
