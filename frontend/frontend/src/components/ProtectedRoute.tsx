import type { ReactElement } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

export default function ProtectedRoute({ children }: { children: ReactElement }) {
  const { user, loading } = useAuth();

  if (loading) return <div className="page-shell muted">Loading...</div>;
  if (!user) return <Navigate to="/login" replace />;

  return children;
}
