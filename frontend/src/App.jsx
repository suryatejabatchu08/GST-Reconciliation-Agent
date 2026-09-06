// frontend/src/App.jsx
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "./contexts/AuthContext";
import PrivateRoute from "./components/PrivateRoute";
import Navbar from "./components/Navbar";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import Upload from "./pages/Upload";
import Jobs from "./pages/Jobs";
import Reconciliation from "./pages/Reconciliation";

function AppLayout({ children }) {
  return (
    <div className="layout">
      <Navbar />
      <main className="main-content">{children}</main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          {/* Public */}
          <Route path="/login" element={<Login />} />

          {/* Protected */}
          <Route path="/dashboard" element={
            <PrivateRoute>
              <AppLayout><Dashboard /></AppLayout>
            </PrivateRoute>
          }/>
          <Route path="/upload" element={
            <PrivateRoute>
              <AppLayout><Upload /></AppLayout>
            </PrivateRoute>
          }/>
          <Route path="/jobs" element={
            <PrivateRoute>
              <AppLayout><Jobs /></AppLayout>
            </PrivateRoute>
          }/>
          <Route path="/jobs/:jobId" element={
            <PrivateRoute>
              <AppLayout><Reconciliation /></AppLayout>
            </PrivateRoute>
          }/>

          {/* Default */}
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}
