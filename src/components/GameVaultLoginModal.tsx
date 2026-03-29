import { FC, useState } from "react";
import { useTranslation } from "react-i18next";
import { ConfirmModal, DialogButton, TextField, Focusable } from "@decky/ui";
import { FaServer } from "react-icons/fa";

interface GameVaultLoginModalProps {
  onLogin: (serverUrl: string, username: string, password: string) => Promise<void>;
  closeModal?: () => void;
}

export const GameVaultLoginModal: FC<GameVaultLoginModalProps> = ({
  onLogin,
  closeModal,
}) => {
  const { t } = useTranslation();
  const [serverUrl, setServerUrl] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const canSubmit = serverUrl.trim() && username.trim() && password.trim() && !loading;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setLoading(true);
    setError("");
    try {
      await onLogin(serverUrl.trim(), username.trim(), password);
      closeModal?.();
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <style>{`
        .gv-login-modal + div { display: none !important; }
        .DialogFooter { display: none !important; }
      `}</style>
      <ConfirmModal
        strTitle={t("gamevaultAuth.title")}
        strDescription=""
        bHideCloseIcon={false}
        onOK={closeModal}
        onCancel={closeModal}
      >
        <Focusable className="gv-login-modal" style={{ display: "flex", flexDirection: "column", gap: "12px", padding: "10px 0" }}>
          {/* Server URL */}
          <div>
            <div style={{ fontSize: "12px", color: "#999", marginBottom: "4px" }}>
              {t("gamevaultAuth.serverUrl")}
            </div>
            <TextField
              value={serverUrl}
              onChange={(e) => setServerUrl(e.currentTarget.value)}
              focusOnMount={true}
              bShowClearAction={true}
            />
          </div>

          {/* Username */}
          <div>
            <div style={{ fontSize: "12px", color: "#999", marginBottom: "4px" }}>
              {t("gamevaultAuth.username")}
            </div>
            <TextField
              value={username}
              onChange={(e) => setUsername(e.currentTarget.value)}
            />
          </div>

          {/* Password */}
          <div>
            <div style={{ fontSize: "12px", color: "#999", marginBottom: "4px" }}>
              {t("gamevaultAuth.password")}
            </div>
            <TextField
              value={password}
              onChange={(e) => setPassword(e.currentTarget.value)}
              bIsPassword={true}
            />
          </div>

          {/* Error message */}
          {error && (
            <div style={{ color: "#ef4444", fontSize: "13px" }}>
              {error}
            </div>
          )}

          {/* Sign In button */}
          <DialogButton
            onClick={handleSubmit}
            disabled={!canSubmit}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "8px",
              marginTop: "4px",
            }}
          >
            <FaServer />
            {loading ? t("gamevaultAuth.signingIn") : t("gamevaultAuth.signIn")}
          </DialogButton>
        </Focusable>
      </ConfirmModal>
    </>
  );
};

export default GameVaultLoginModal;
