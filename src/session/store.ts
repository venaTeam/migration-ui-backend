export interface UserSession {
  grafanaUrl: string;
  grafanaUsername: string;
  grafanaPassword: string;
  createdAt: number;
  lastUsedAt: number;
}

const sessions = new Map<string, UserSession>();

export function createSession(sessionId: string, session: Omit<UserSession, "createdAt" | "lastUsedAt">): void {
  const now = Date.now();
  sessions.set(sessionId, {
    ...session,
    createdAt: now,
    lastUsedAt: now,
  });
}

export function getSession(sessionId: string): UserSession | undefined {
  const session = sessions.get(sessionId);
  if (session) {
    session.lastUsedAt = Date.now();
  }
  return session;
}

export function deleteSession(sessionId: string): void {
  sessions.delete(sessionId);
}

export function touchSession(sessionId: string): void {
  const session = sessions.get(sessionId);
  if (session) {
    session.lastUsedAt = Date.now();
  }
}
