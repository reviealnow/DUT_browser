/**
 * Session state for the whole app (P71b).
 *
 * The landing model is guest-by-default: an anonymous browser is a first-class
 * state that sees the guest sections, and only engineers/admins ever log in.
 * `role` is therefore never null — no session simply means "guest".
 *
 * Enforcement lives in the backend; everything here (nav filtering, section
 * guards) is cosmetic UX on top of the real 401/403s.
 */
import {
  createContext,
  ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  AUTH_UNAUTHORIZED_EVENT,
  AuthUser,
  getMe,
  logoutAuth,
  registerAuth,
  RegisterParams,
  Role,
} from "../api/rest";

export const ROLE_RANK: Record<Role, number> = { guest: 0, engineer: 1, admin: 2 };

export type AuthState = {
  /** Logged-in identity, or null for the anonymous guest browser. */
  user: AuthUser | null;
  /** Effective role for UI decisions; anonymous browsers act as guest. */
  role: Role;
  /** True until the initial /api/auth/me check resolves. */
  loading: boolean;
  login: (params: RegisterParams) => Promise<AuthUser>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  // Serializes refreshes so a burst of 401s (one per gated call on a page)
  // collapses into a single in-flight /me check.
  const refreshing = useRef<Promise<void> | null>(null);

  const refresh = useCallback(() => {
    if (!refreshing.current) {
      refreshing.current = getMe()
        .then(setUser)
        .catch(() => {
          // /me unreachable (backend down): keep the current state rather than
          // logging the user out over a blip.
        })
        .finally(() => {
          refreshing.current = null;
        });
    }
    return refreshing.current;
  }, []);

  useEffect(() => {
    void refresh().then(() => setLoading(false));
  }, [refresh]);

  // Any 401 from the REST layer MIGHT mean the session expired server-side.
  // Re-check /me before believing it: guest browsers legitimately collect 401s
  // from engineer-gated endpoints, and those must not flip any state.
  useEffect(() => {
    const onUnauthorized = () => void refresh();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
  }, [refresh]);

  const login = useCallback(async (params: RegisterParams) => {
    const logged = await registerAuth(params);
    setUser(logged);
    return logged;
  }, []);

  const logout = useCallback(async () => {
    await logoutAuth();
    setUser(null);
  }, []);

  const role: Role = user?.role ?? "guest";
  return (
    <AuthContext.Provider value={{ user, role, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const state = useContext(AuthContext);
  if (!state) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return state;
}
