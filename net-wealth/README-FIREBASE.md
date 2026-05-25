# Firebase Cloud Sync for ODIN

Real-time cross-device sync for ODIN net-worth tracker. Changes on macbook appear instantly on mobile (and vice versa).

## Setup (5 min)

### 1. Create Firebase Project
1. Go to [console.firebase.google.com](https://console.firebase.google.com)
2. Click **Create Project** → name it `odin-net-worth`
3. Enable Google Analytics (optional)
4. Click **Create**

### 2. Enable Firestore
1. In Firebase Console → **Firestore Database**
2. Click **Create Database**
3. Choose **Test mode** (allows reads/writes; fine for personal use)
4. Select region (us-central1 recommended)
5. Click **Create**

### 3. Enable Authentication
1. Firebase Console → **Authentication**
2. Click **Get Started**
3. Select **Anonymous** authentication provider
4. Click **Enable** → **Save**

### 4. Get Configuration
1. Firebase Console → **Project Settings** (gear icon)
2. Click **Your apps** → **Web** (</> icon)
3. Copy the firebaseConfig object

### 5. Update firebase-config.js
1. Open `net-wealth/firebase-config.js`
2. Replace `YOUR_API_KEY_HERE` etc. with your config:
```javascript
const FIREBASE_CONFIG = {
  apiKey: "YOUR_API_KEY",
  authDomain: "your-project.firebaseapp.com",
  projectId: "your-project-id",
  storageBucket: "your-project.appspot.com",
  messagingSenderId: "YOUR_SENDER_ID",
  appId: "YOUR_APP_ID"
};
```
3. **Save** and reload ODIN

### 6. Set Firestore Rules (optional)
For tighter security, update Firestore Rules in Firebase Console:
```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    match /users/{uid}/data/{document=**} {
      allow read, write: if request.auth.uid == uid || request.auth == null;
    }
  }
}
```

## How It Works

- **Inputs** (asset values, loan amounts) → saved to Firestore
- **History** (daily net-worth snapshots) → saved to Firestore
- **Real-time listeners** → pulls changes from other devices every few seconds
- **Fallback to localStorage** → if Firebase unavailable, works offline

## Cross-Device Sync

### Macbook → Mobile
1. Edit asset value on macbook, click **SAVE**
2. Appears instantly on mobile (within ~1-2 seconds)
3. Browser shows **☁️ Synced to Firebase** confirmation

### Mobile → Macbook
Same flow, opposite direction. No manual steps needed.

## Checking Sync Status

- Green **☁️ Synced to Firebase** message = data backed up
- "**Saved locally**" message = Firebase not ready yet (will retry)

## Data Privacy

- **Anonymous auth**: No email/password required
- Each device gets unique ID automatically
- Data stored in your own Firebase project (you control it)
- Can view/delete data in Firebase Console → Firestore Database

## Troubleshooting

**"Firebase SDK not loaded"**
- Check browser console (F12 → Console)
- Reload page
- Check that firebase-config.js loads without errors

**Sync not working**
- Verify `firebase-config.js` has correct credentials
- Check Firestore is enabled in Firebase Console
- Check Firestore Rules allow anonymous access

**Offline?**
- App still works with localStorage as fallback
- Syncs when internet returns

## Local Development

To test Firebase locally before deploying:
```bash
npm install -g firebase-tools
firebase init  # use existing project "odin-net-worth"
firebase serve
```

Then open `http://localhost:5000/net-wealth/` in two browser windows to test cross-device sync.

## Cost

Firebase has a generous free tier (Firestore: 50K reads/day, 20K writes/day). Personal net-worth tracking easily stays under free limits.
