/**
 * Local-only helpers for the recorder (borrowed from ecosystem/e2e/harness): a throwaway Postgres 17 cluster on 127.0.0.1
 * (no unix socket, a free port, a database whose name ends in `test`) and PayBroker itself, with no AI account configured.
 * Nothing here reads a real environment: the app gets an explicit environment and a private working directory.
 */
import { ChildProcess, execFileSync, spawn } from 'node:child_process';
import * as fs from 'node:fs';
import * as net from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';

export const REPO = path.resolve(__dirname, '..', '..');
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** A free loopback port. PB_PORT_RANGE=41700-41799 restricts the choice (used when ports are reserved); default: any. */
export async function freePort(): Promise<number> {
  const m = /^(\d+)-(\d+)$/.exec(process.env.PB_PORT_RANGE ?? '');
  const tryPort = (port: number) =>
    new Promise<number | undefined>((resolve) => {
      const srv = net.createServer();
      srv.unref();
      srv.on('error', () => resolve(undefined));
      srv.listen(port, '127.0.0.1', () => {
        const got = (srv.address() as net.AddressInfo).port;
        srv.close(() => resolve(got));
      });
    });
  if (!m) return (await tryPort(0)) as number;
  const lo = Number(m[1]);
  const hi = Number(m[2]);
  for (let i = 0; i < 200; i++) {
    const port = lo + Math.floor(Math.random() * (hi - lo + 1));
    const got = await tryPort(port);
    if (got && !used.has(got)) {
      used.add(got);
      return got;
    }
  }
  throw new Error(`no free port in ${lo}-${hi}`);
}
const used = new Set<number>();

export async function waitFor<T>(what: string, fn: () => Promise<T | false | undefined> | T | false | undefined, ms: number): Promise<T> {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v as T;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await sleep(250);
  }
}

/** Directory with initdb, postgres, psql (pgvector and pg_trgm installed). PB_PG_BIN overrides; else whatever is on PATH. */
function pgBin(): string {
  if (process.env.PB_PG_BIN) return process.env.PB_PG_BIN;
  try {
    return path.dirname(execFileSync('which', ['initdb'], { encoding: 'utf8' }).trim());
  } catch {
    throw new Error('Postgres binaries not found: put initdb on PATH or set PB_PG_BIN to their directory');
  }
}

export interface Cluster {
  url: string;
  /** Same database as the login role `reporting_reader` (a member of a1p_readonly, like `agentpg db-users create --permission read`). */
  readerUrl: string;
  port: number;
  dbName: string;
  bin: string;
  stop(): void;
}

export async function startCluster(dbName: string): Promise<Cluster> {
  if (!/^[a-z0-9_]*test$/.test(dbName)) throw new Error('the throwaway database name must end in "test"');
  const bin = pgBin();
  const env = { PATH: `${bin}:/usr/bin:/bin`, LANG: 'C', LC_ALL: 'C' };
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'paybroker-video-pg-'));
  const data = path.join(dir, 'data');
  const port = await freePort();
  execFileSync(path.join(bin, 'initdb'), ['-D', data, '-E', 'UTF8', '--locale=C', '-A', 'trust', '-U', 'postgres'], { env, stdio: 'pipe' });
  const log = fs.openSync(path.join(dir, 'postgres.log'), 'a');
  const proc: ChildProcess = spawn(
    path.join(bin, 'postgres'),
    ['-D', data, '-c', 'listen_addresses=127.0.0.1', '-c', `port=${port}`, '-c', 'unix_socket_directories=', '-c', 'fsync=off', '-c', 'max_connections=100', '-c', 'shared_preload_libraries=pg_stat_statements'],
    { env, stdio: ['ignore', log, log] },
  );
  const psql = (db: string, sql: string) =>
    execFileSync(path.join(bin, 'psql'), ['-h', '127.0.0.1', '-p', String(port), '-U', 'postgres', '-d', db, '-v', 'ON_ERROR_STOP=1', '-c', sql], { env, stdio: 'pipe' });
  const stop = () => {
    try {
      proc.kill('SIGINT');
    } catch {}
    try {
      execFileSync(path.join(bin, 'pg_ctl'), ['-D', data, 'stop', '-m', 'immediate', '-s'], { env, stdio: 'pipe', timeout: 15_000 });
    } catch {}
    try {
      proc.kill('SIGKILL');
    } catch {}
    fs.rmSync(dir, { recursive: true, force: true });
  };
  try {
    await waitFor('postgres', () => {
      try {
        execFileSync(path.join(bin, 'pg_isready'), ['-h', '127.0.0.1', '-p', String(port)], { env, stdio: 'pipe' });
        return true;
      } catch {
        return false;
      }
    }, 30_000);
    psql('postgres', `CREATE DATABASE ${dbName} ENCODING 'UTF8' TEMPLATE template0`);
    for (const ext of ['vector', 'pg_trgm', 'pg_stat_statements']) psql(dbName, `CREATE EXTENSION IF NOT EXISTS ${ext}`);
    // What AgentPG provides on every instance: the group role a1p_readonly (SELECT on new public tables by default) and a login
    // role that belongs to it. Created BEFORE `paybroker migrate`, whose after-migrate hook then takes the default away (hardening.py).
    psql('postgres', `CREATE ROLE a1p_readonly NOLOGIN`);
    psql('postgres', `CREATE ROLE reporting_reader LOGIN`);
    psql('postgres', `GRANT a1p_readonly TO reporting_reader`);
    psql(dbName, `GRANT USAGE ON SCHEMA public TO a1p_readonly`);
    psql(dbName, `ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO a1p_readonly`);
  } catch (e) {
    stop();
    throw e;
  }
  return { url: `postgresql://postgres@127.0.0.1:${port}/${dbName}`, readerUrl: `postgresql://reporting_reader@127.0.0.1:${port}/${dbName}`, port, dbName, bin, stop };
}

export interface Running {
  url: string;
  logs(): string;
  stop(): Promise<void>;
}

const BASE_ENV = (home: string) => ({ PATH: '/usr/bin:/bin', HOME: home, TMPDIR: home, LANG: 'en_US.UTF-8', PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1', NO_COLOR: '1', HOST: '127.0.0.1' });

export function venvBin(name: string): string {
  const p = path.join(REPO, '.venv', 'bin', name);
  if (!fs.existsSync(p)) throw new Error(`${p} not found: run  uv venv && uv pip install -e ".[dev]"  in the PayBroker repository first`);
  return p;
}

function launch(cmd: string, args: string[], env: Record<string, string>, cwd: string, url: string, ready: string): Promise<Running> {
  const p = spawn(cmd, args, { env, cwd, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
  let buf = '';
  let exited: number | undefined;
  p.stdout!.on('data', (d) => (buf += d));
  p.stderr!.on('data', (d) => (buf += d));
  p.on('exit', (c) => (exited = c ?? -1));
  const running: Running = {
    url,
    logs: () => buf,
    async stop() {
      if (exited !== undefined) return;
      try {
        process.kill(-p.pid!, 'SIGTERM');
      } catch {}
      const end = Date.now() + 8000;
      while (exited === undefined && Date.now() < end) await sleep(100);
      if (exited === undefined) {
        try {
          process.kill(-p.pid!, 'SIGKILL');
        } catch {}
      }
    },
  };
  return waitFor(`${path.basename(cmd)} ${ready}`, async () => {
    if (exited !== undefined) throw new Error(`${cmd} exited early (${exited})\n${buf.slice(-2000)}`);
    try {
      return (await fetch(url + ready)).ok;
    } catch {
      return false;
    }
  }, 90_000).then(() => running, async (e) => {
    await running.stop();
    throw e;
  });
}

export interface Stack {
  url: string;
  port: number;
  /** One-time setup code the server printed (it belongs to the throwaway server and is used up when the video ends). */
  setupCode: string;
  serverLog(): string;
  cluster: Cluster;
  stop(): Promise<void>;
}

/** A throwaway database and PayBroker, all on loopback, in a private working directory. A fresh database per take. */
export async function startStack(): Promise<Stack> {
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'paybroker-video-'));
  const cluster = await startCluster('paybroker_video_test');
  const port = await freePort();
  const url = `http://localhost:${port}`;
  const env = {
    ...BASE_ENV(work),
    DATABASE_URL: cluster.url,
    PORT: String(port),
    PAYBROKER_PUBLIC_URL: url,
    PAYBROKER_ALLOW_PRIVATE_URLS: 'true', // the webhook receiver is on 127.0.0.1: local testing only
    // no LLM_BASE_URL: the built-in rule-based triage writes the case summary, as the script narrates
  };
  const bin = venvBin('paybroker');
  execFileSync(bin, ['migrate'], { env, cwd: work, stdio: 'pipe' });
  let app: Running | undefined;
  const cleanup = async () => {
    await app?.stop();
    cluster.stop();
    fs.rmSync(work, { recursive: true, force: true });
  };
  try {
    app = await launch(bin, ['serve'], env, work, `http://127.0.0.1:${port}`, '/readyz');
    const code = await waitFor('the setup code', () => app!.logs().match(/\b([A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})\b/)?.[1], 20_000);
    return { url, port, setupCode: code, serverLog: () => app!.logs(), cluster, stop: cleanup };
  } catch (e) {
    await cleanup();
    throw e;
  }
}
