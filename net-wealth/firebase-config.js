// Firebase Configuration — FILLED IN
// Get these from Firebase Console: Project Settings > Your apps > Web
const FIREBASE_CONFIG = {
  apiKey: "AIzaSyApWqRinHrG___uLckBB47H09LHVwQ0ya8",
  authDomain: "odin-a9ce7.firebaseapp.com",
  projectId: "odin-a9ce7",
  storageBucket: "odin-a9ce7.firebasestorage.app",
  messagingSenderId: "461548506429",
  appId: "1:461548506429:web:2d0ba662c2834334af4954",
  measurementId: "G-S0N0FKP7Z3"
};

// Firebase Sync Module
const FirebaseSync = {
  uid: null,
  db: null,
  auth: null,
  isReady: false,
  listeners: [],

  async init() {
    return new Promise((resolve) => {
      if (!window.firebase) {
        console.error('Firebase SDK not loaded');
        resolve(false);
        return;
      }
      try {
        firebase.initializeApp(FIREBASE_CONFIG);
        this.db = firebase.firestore();
        this.auth = firebase.auth();

        // Use hardcoded shared UID for all devices
        this.uid = 'shared-all-devices';
        this.isReady = true;
        console.log('🔥 Firebase ready (shared UID):', this.uid);
        resolve(true);
      } catch (err) {
        console.error('Firebase init failed:', err);
        resolve(false);
      }
    });
  },

  // ── Single-doc state sync (post-refactor: whole seed snapshot) ──
  async saveState(state) {
    if (!this.isReady || !this.uid) return false;
    try {
      await this.db.collection('users').doc(this.uid).collection('data').doc('state').set({
        snapshot: state,
        synced_at: firebase.firestore.FieldValue.serverTimestamp()
      });
      return true;
    } catch (err) {
      console.error('saveState failed:', err);
      return false;
    }
  },

  async loadState() {
    if (!this.isReady || !this.uid) return null;
    try {
      const doc = await this.db.collection('users').doc(this.uid).collection('data').doc('state').get();
      return doc.exists ? (doc.data().snapshot || null) : null;
    } catch (err) {
      console.error('loadState failed:', err);
      return null;
    }
  },

  async addHistoryEntry(entry) {
    if (!this.isReady || !this.uid) return;
    try {
      const timestamp = new Date().toISOString();
      await this.db.collection('users').doc(this.uid).collection('data').doc('history')
        .collection('entries').doc(timestamp).set({
          ...entry,
          created_at: firebase.firestore.FieldValue.serverTimestamp()
        });
    } catch (err) {
      console.error('addHistoryEntry failed:', err);
    }
  },

  async loadHistory(days = 90) {
    if (!this.isReady || !this.uid) return [];
    try {
      const cutoff = new Date();
      cutoff.setDate(cutoff.getDate() - days);

      const snap = await this.db.collection('users').doc(this.uid).collection('data')
        .doc('history').collection('entries')
        .where('created_at', '>=', cutoff)
        .orderBy('created_at', 'asc')
        .limit(200)
        .get();

      return snap.docs.map(d => {
        const data = d.data();
        return {
          date: data.date || new Date(data.created_at.toDate()).toISOString().split('T')[0],
          net_worth: data.net_worth || 0,
          assets: data.assets || 0,
          liab: data.liab || 0,
          fire_state: data.fire_state || {},
          snapshot_note: data.snapshot_note || ''
        };
      });
    } catch (err) {
      console.error('loadHistory failed:', err);
      return [];
    }
  },

  async saveFireParams(params) {
    if (!this.isReady || !this.uid) return false;
    try {
      await this.db.collection('users').doc(this.uid).collection('data').doc('fire_params').set(params, { merge: true });
      return true;
    } catch (err) {
      console.error('saveFireParams failed:', err);
      return false;
    }
  },

  async loadFireParams() {
    if (!this.isReady || !this.uid) return {};
    try {
      const doc = await this.db.collection('users').doc(this.uid).collection('data').doc('fire_params').get();
      return doc.exists ? doc.data() : {};
    } catch (err) {
      console.error('loadFireParams failed:', err);
      return {};
    }
  },

  onFireParamsChange(callback) {
    if (!this.isReady || !this.uid) return () => {};
    const unsub = this.db.collection('users').doc(this.uid).collection('data').doc('fire_params')
      .onSnapshot(doc => {
        callback(doc.exists ? doc.data() : {});
      }, err => console.error('fire_params listener error:', err));
    this.listeners.push(unsub);
    return unsub;
  },

  unsubscribeAll() {
    this.listeners.forEach(unsub => unsub());
    this.listeners = [];
  }
};
