// In-memory PostgreSQL fixture; never starts a server or uses production credentials.
import { createInterface } from "node:readline";
const modulePath = process.env.WRIGHT_ARCTIC_PGLITE_MODULE ?? new URL(
  "./node_modules/@electric-sql/pglite/dist/index.js", import.meta.url,
).pathname;
const { PGlite } = await import(modulePath);
const db = new PGlite();
const decode = value => {
  if (value && typeof value === "object" && "$bytes" in value) return Uint8Array.from(Buffer.from(value.$bytes, "base64"));
  return value;
};
const encode = (_key, value) => value instanceof Uint8Array ? { $bytes: Buffer.from(value).toString("base64") } : typeof value === "bigint" ? String(value) : value;
await db.exec(`create role anon nologin; create role authenticated nologin;
  create schema auth; create table auth.users(id uuid primary key);
  create table public.profiles(id uuid primary key references auth.users(id),username text not null unique);`);
if (!process.argv.includes("--empty")) {
  await db.exec(await Bun.file(new URL("../services/documents/sql/20261107000600_documents.sql", import.meta.url)).text());
}
console.log(JSON.stringify({ ready: true }));
for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  try {
    const command = JSON.parse(line);
    const result = command.parameters?.length
      ? await db.query(command.sql, command.parameters.map(decode))
      : { rows: (await db.exec(command.sql))[0]?.rows ?? [] };
    console.log(JSON.stringify({ rows: result.rows }, encode));
  } catch (error) {
    console.log(JSON.stringify({ error: { message: error.message, code: error.code } }));
  }
}
await db.close();
