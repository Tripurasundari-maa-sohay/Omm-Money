// Firebase Configuration — FILL IN YOUR CREDENTIALS
// Get these from Firebase Console: Project Settings > Your apps > Web
const FIREBASE_CONFIG = {
  apiKey: "YOUR_API_KEY_HERE",
  authDomain: "your-project.firebaseapp.com",
  projectId: "your-project-id",
  storageBucket: "your-project.appspot.com",
  messagingSenderId: "YOUR_SENDER_ID",
  appId: "YOUR_APP_ID"
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

        this.auth.onAuthStateChanged(user => {
          if (user) {
            this.uid = user.uid;
          } else {
            // Anonymous auth
            this.auth.signInAnonymously().catch(err => {
              this.uid = 'anon_' + Math.random().toString(36).slice(2, 9);
              console.warn('Anon auth failed, using device ID:', this.uid);
            });
            this.uid = this.uid || ('anon_' + Math.random().toString(36).slice(2, 9));
          }
          this.isReady = true;
          console.log('🔥 Firebase ready:', this.uid);
          resolve(true);
        });
      } catch (err) {
        console.error('Firebase init failed:', err);
        resolve(false);
      }
    });
  },

  async saveInputs(inputs) {
    if (!this.isReady || !this.uid) return false;
    try {
      await this.db.collection('users').doc(this.uid).collection('data').doc('inputs').set({
        ...inputs,
        synced_at: firebase.firestore.FieldValue.serverTimestamp()
      }, { merge: true });
      return true;
    } catch (err) {
      console.error('saveInputs failed:', err);
      return false;
    }
  },

  async loadInputs() {
    if (!this.isReady || !this.uid) return {};
    try {
      const doc = await this.db.collection('users').doc(this.uid).collection('data').doc('inputs').get();
      return doc.exists ? doc.data() : {};
    } catch (err) {
      console.error('loadInputs failed:', err);
      return {};
    }
  },

  onInputsChange(callback) {
    if (!this.isReady || !this.uid) return () => {};
    const unsub = this.db.collection('users').doc(this.uid).collection('data').doc('inputs')
      .onSnapshot(doc => {
        callback(doc.exists ? doc.data() : {});
      }, err => console.error('inputs listener error:', err));
    this.listeners.push(unsub);
    return unsub;
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
