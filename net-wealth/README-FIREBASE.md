# Firebase Cloud Sync for ODIN

Real-time cross-device sync for ODIN net-worth tracker. Changes on macbook appear instantly on mobile (and vice versa).

## ✅ Implementation Status (May 2026)

**Fully functional cross-device sync with:**
- Shared UID sync across all devices (no per-device isolation)
- Real-time Firebase listeners for instant updates
- Portfolio FX rates integrated (shared source with portfolio tracker)
- Gold rate overrides (jewellery & malabar) synced to Firebase
- Excel export/import for bulk data management
- All financial values formatted with 2 decimals
- Insurance renewal reminders (health & term)

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
2. Replace credentials with your Firebase project config
3. Save and reload ODIN

### 6. Deploy Firestore Rules
1. Create `firestore.rules` file (template provided)
2. Deploy via Firebase CLI:
```bash
firebase deploy --only firestore:rules
```

## How It Works

### Data Storage
- **Inputs** (asset values, loans, gold rates) → Firestore path: `/users/shared-all-devices/data/inputs`
- **History** (daily snapshots) → Firestore path: `/users/shared-all-devices/data/history/entries`
- **Real-time listeners** → pull changes from Firestore every few seconds
- **Fallback to localStorage** → if Firebase unavailable, works offline

### Sync Flow
1. User edits form field → clicks **SAVE**
2. Value saved to localStorage
3. Syncs to Firebase `/users/shared-all-devices/data/`
4. Firebase real-time listener detects change
5. ALL devices receive update within 1-2 seconds
6. Mobile refreshes → shows updated value

## Features

### Cross-Device Sync
- **Shared UID**: All devices write to same Firebase path (`shared-all-devices`)
- **No device isolation**: Changes on one device appear instantly on all others
- **Portfolio sync**: Portfolio values (India/US stocks, gold rates) included in sync
- **Gold rate overrides**: Jewellery (₹/10g) and Malabar (QAR/g) auto-convert and sync

### Data Management
- **Excel Export** (📊 button): Download current asset values as ODIN-Data.xlsx
- **Excel Import** (📥 button): Upload Excel file to populate form fields
- **Currency support**: INR, USD, QAR with automatic conversions
- **2-decimal formatting**: All financial values display with .00 precision

### FX Rates
- **Source**: Portfolio tracker's `market_indices.json` (daily updated)
- **USD/INR**: Loaded from portfolio's dual-API fallback (open.er-api.com + exchangerate-api.com)
- **QAR/INR**: Calculated using fixed peg (3.6413 QAR/USD)
- **Auto-update**: Syncs daily when portfolio data updates

### Insurance Reminders
- **Health Insurance**: Expires 27/08/2026 → renewal reminder in TO-DO
- **Term Insurance**: Annual renewal every March 31 → next due 03/2027
- Both tracked in TO-DO list with due dates

### Gold Rate Overrides
Users can override gold rates in ASSETS tab:
- **Jewellery**: Enter price per 10 grams (₹/10g) → auto-converts to ₹/g internally
- **Malabar**: Enter price in QAR/g → auto-converts to INR/g using live FX rate
- Both values synced to Firebase and all devices

## Sync Status

### Success Message
```
☁️ Synced to Firebase
```
Data backed up to Firebase, visible on all devices.

### Fallback Message
```
💾 Saved to localStorage
```
Firebase not ready yet (will retry), data safe in local storage.

## Data Privacy

- **Shared UID**: All devices write to same Firebase path for unified data
- **Anonymous auth**: No email/password required
- **Firestore Rules**: Restrict access to authenticated users or allow unauthenticated access
- **Your project**: You control the Firebase project and all data within it

## Checking Sync

1. **Console logs** (F12 → Console):
   - "🔥 Firebase ready (shared UID): shared-all-devices" = connected
   - "💱 FX rates: USD/INR=XX.XX, QAR/INR=YY.YY" = FX loaded
   - "Inputs synced from another device" = real-time listener received update

2. **Firebase Console**:
   - Firestore Database → `users/shared-all-devices/data` → see live documents
   - View `inputs`, `history/entries`, `fire_params` collections

## Troubleshooting

**"Firebase ready but not syncing"**
- Check Firestore Rules allow read/write to `users/shared-all-devices/data/*`
- Verify mobile/desktop browsers have same Firebase config
- Check browser network tab for Firestore requests

**"FX rates not loading"**
- Check portfolio's `data/processed/market_indices.json` exists and has `fx_rate` field
- Fallback to seed.json rates if portfolio data unavailable
- Check console for FX load error message

**"Gold rate override not syncing"**
- Verify override input appears in ASSETS tab gold section
- Check console for "Inputs synced from another device" message
- Reload mobile browser to fetch updated rates

**Offline?**
- App works with localStorage as fallback
- Syncs to Firebase when internet returns
- Check "Saved locally" message in console

## File Structure

```
net-wealth/
├── index.html                    # Main app (all UI + logic)
├── firebase-config.js            # Firebase SDK + sync module
├── firestore.rules               # Firestore security rules
├── firebase.json                 # Firebase CLI config
├── .firebaserc                   # Firebase project ID
├── data/
│   └── seed.json                 # Initial data (assets, banks, todos, FX rates)
├── manifest.json                 # PWA manifest
├── sw.js                         # Service worker (offline support)
└── README-FIREBASE.md            # This file
```

## Key Implementation Details

### Shared UID Approach
Instead of per-device anonymous auth (which created device isolation), all devices use hardcoded `shared-all-devices` UID:
```javascript
// firebase-config.js
this.uid = 'shared-all-devices';
```

This ensures all devices write to same Firebase path, enabling true cross-device sync.

### Firestore Rules
```firestore
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    // Allow all devices to read/write shared data
    match /users/shared-all-devices/data/{document=**} {
      allow read, write: if true;
    }
  }
}
```

### Data Models

**Inputs** (current values):
```json
{
  "fo_corpus_cash": 1818693.78,
  "fo_margin_used": 5706715.55,
  "gold_jewellery_grams": 471,
  "jewel_22k_rate_inr_per_g": 14580,
  "malabar_grams": 68.4,
  "malabar_rate_inr_per_g": 11925.5,
  "apartment_market_value": 6500000,
  ...
}
```

**History** (snapshots with timestamp):
```json
{
  "date": "2026-05-25",
  "net_worth": 126857350,
  "assets": 135234000,
  "liab": 8376650,
  "fire_state": {...},
  "snapshot_note": "Updated all bank balances",
  "created_at": "2026-05-25T14:30:00Z"
}
```

## Local Development

To test Firebase locally:
```bash
npm install -g firebase-tools
firebase init  # use existing project
firebase serve
```

Then test sync by opening `http://localhost:5000/net-wealth/` in two browser windows.

## Cost

Firebase free tier includes:
- Firestore: 50K reads/day, 20K writes/day
- Authentication: unlimited anonymous users
- Storage: 5GB

Personal net-worth tracking easily stays under free limits.

## Future Enhancements

- [ ] Auto-snapshot on schedule (daily)
- [ ] Trend analysis dashboard (chart net worth over time)
- [ ] Multi-user support (share data with spouse)
- [ ] Mobile app (iOS/Android PWA)
- [ ] Offline sync queue (sync pending changes when online)

---

**Last Updated**: May 25, 2026
**Firebase Project**: odin-a9ce7
**Sync Type**: Shared UID (all devices unified)
**Status**: ✅ Production Ready
