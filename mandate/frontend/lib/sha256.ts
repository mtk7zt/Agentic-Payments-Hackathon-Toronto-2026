// Synchronous SHA-256, so an event's hash can be computed inside the same
// ledger transaction that writes it. Verification re-hashes with the browser's
// own Web Crypto implementation, so the two are checked against each other.

function primes(n: number): number[] {
  const out: number[] = [];
  for (let c = 2; out.length < n; c++) if (out.every((p) => c % p !== 0)) out.push(c);
  return out;
}
const frac32 = (x: number) => Math.floor((x - Math.floor(x)) * 4294967296) >>> 0;
const P = primes(64);
const K = new Uint32Array(P.map((p) => frac32(Math.cbrt(p))));
const H0 = P.slice(0, 8).map((p) => frac32(Math.sqrt(p)));
const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));

export function sha256(message: string): string {
  const bytes = new TextEncoder().encode(message);
  const len = bytes.length;
  const padded = ((len + 9 + 63) >> 6) << 6;
  const buf = new Uint8Array(padded);
  buf.set(bytes);
  buf[len] = 0x80;
  const dv = new DataView(buf.buffer);
  dv.setUint32(padded - 8, Math.floor((len * 8) / 4294967296));
  dv.setUint32(padded - 4, (len * 8) >>> 0);

  const h = new Uint32Array(H0);
  const w = new Uint32Array(64);
  for (let i = 0; i < padded; i += 64) {
    for (let t = 0; t < 16; t++) w[t] = dv.getUint32(i + t * 4);
    for (let t = 16; t < 64; t++) {
      const s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18) ^ (w[t - 15] >>> 3);
      const s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19) ^ (w[t - 2] >>> 10);
      w[t] = (w[t - 16] + s0 + w[t - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let t = 0; t < 64; t++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (hh + S1 + ch + K[t] + w[t]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      hh = g;
      g = f;
      f = e;
      e = (d + t1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (t1 + t2) >>> 0;
    }
    h[0] += a;
    h[1] += b;
    h[2] += c;
    h[3] += d;
    h[4] += e;
    h[5] += f;
    h[6] += g;
    h[7] += hh;
  }
  return Array.from(h, (x) => x.toString(16).padStart(8, "0")).join("");
}

export async function sha256WebCrypto(message: string): Promise<string | null> {
  if (typeof crypto === "undefined" || !crypto.subtle) return null;
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(message));
  return Array.from(new Uint8Array(d), (b) => b.toString(16).padStart(2, "0")).join("");
}
