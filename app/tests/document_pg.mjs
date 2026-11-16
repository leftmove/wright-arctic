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
await db.exec(`create role anon nologin; create role authenticated nologin;`);
const notices = [];
if (!process.argv.includes("--empty")) {
  const ledger = new URL("../services/catalog/sql/", import.meta.url).pathname;
  for (const migration of [...new Bun.Glob("*_*.sql").scanSync(ledger)].sort()) {
    await db.exec(await Bun.file(ledger + migration).text());
  }
  await db.listen("arctic_catalog_changes", number => notices.push(Number(number)));
}
console.log(JSON.stringify({ ready: true }));
for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  try {
    const command = JSON.parse(line);
    if (command.notifications) {
      await db.query("select 1");
      await new Promise(resolve => setTimeout(resolve, 0));
      console.log(JSON.stringify(notices.splice(0)));
      continue;
    }
    const result = command.parameters?.length
      ? await db.query(command.sql, command.parameters.map(decode))
      : { rows: (await db.exec(command.sql))[0]?.rows ?? [] };
    console.log(JSON.stringify({ rows: result.rows }, encode));
  } catch (error) {
    console.log(JSON.stringify({ error: { message: error.message, code: error.code, detail: error.detail } }));
  }
}
await db.close();
