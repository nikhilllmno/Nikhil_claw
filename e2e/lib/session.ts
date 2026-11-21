// Tokens of the test users for the tests. Each worker signs a user in once and again only
// when the token has less than 10 minutes left, so the run stays under the Supabase rate limit.

import { signIn, type Tokens } from "./supabase.ts";
import type { World } from "./world.ts";

const MARGIN_MS = 10 * 60_000;

export class TokenCache {
  private readonly cache = new Map<string, Tokens>();

  private readonly world: World;

  constructor(world: World) {
    this.world = world;
  }

  async tokens(email: string, password: string): Promise<Tokens> {
    const cached = this.cache.get(email);
    if (cached && cached.expires_at - Date.now() > MARGIN_MS) return cached;
    const fresh = await signIn(this.world.supabase, email, password);
    this.cache.set(email, fresh);
    return fresh;
  }

  async forUser(key: string): Promise<Tokens> {
    const user = this.world.users[key];
    if (!user) throw new Error(`The world has no user ${key}.`);
    return this.tokens(user.email, user.password);
  }
}
