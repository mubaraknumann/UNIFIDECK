/**
 * GameVaultCredentialsModal — connection form for a self-hosted
 * GameVault server.
 *
 * Fields:
 *  serverUrl   — HTTP(S) URL of the GameVault server
 *  username    — GameVault account username
 *  password    — GameVault account password
 *  verifySsl   — toggle to skip TLS certificate validation
 *                (useful for self-signed certs on LAN servers)
 *  downloadDir — *separate* temporary directory for archive
 *                downloads.  The game archive is placed here
 *                while downloading, then extracted to the final
 *                install location and deleted.  Keeping this on
 *                a different drive/partition lets users install
 *                to an SSD that doesn't have enough space for
 *                both the archive AND the extracted game at the
 *                same time.
 */
import { FC, useState } from "react";
import { ConfirmModal, TextField, ToggleField, DialogButton } from "@decky/ui";
import { openFilePicker, FileSelectionType } from "@decky/api";
import { useTranslation } from "react-i18next";

interface Props {
  closeModal?: () => void;
  onConnect: (
    serverUrl: string,
    username: string,
    password: string,
    verifySsl: boolean,
    downloadDir: string,
  ) => Promise<void>;
  /** Pre-fill values (e.g. when re-opening an already-configured connection) */
  initialServerUrl?: string;
  initialUsername?: string;
  initialDownloadDir?: string;
  initialVerifySsl?: boolean;
}

export const GameVaultCredentialsModal: FC<Props> = ({
  closeModal,
  onConnect,
  initialServerUrl = "http://",
  initialUsername = "",
  initialDownloadDir = "",
  initialVerifySsl = true,
}) => {
  const { t } = useTranslation();

  const [serverUrl, setServerUrl] = useState(initialServerUrl);
  const [username, setUsername] = useState(initialUsername);
  const [password, setPassword] = useState("");
  const [verifySsl, setVerifySsl] = useState(initialVerifySsl);
  const [downloadDir, setDownloadDir] = useState(initialDownloadDir);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pickDownloadDir = async () => {
    try {
      const res = await openFilePicker(
        FileSelectionType.FOLDER,
        downloadDir || "/home/deck",
        false,
        true,
      );
      const picked = res?.realpath || res?.path;
      if (picked) setDownloadDir(picked);
    } catch {
      // user cancelled
    }
  };

  const handleConnect = async () => {
    if (!serverUrl || serverUrl === "http://" || serverUrl === "https://") {
      setError(t("gamevault.errorServerUrlRequired"));
      return;
    }
    if (!username) {
      setError(t("gamevault.errorUsernameRequired"));
      return;
    }
    if (!password) {
      setError(t("gamevault.errorPasswordRequired"));
      return;
    }

    setError(null);
    setLoading(true);
    try {
      await onConnect(serverUrl, username, password, verifySsl, downloadDir);
      closeModal?.();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("gamevault.errorConnection"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <ConfirmModal
      strTitle={t("gamevault.connectTitle")}
      strOKButtonText={
        loading ? t("gamevault.connecting") : t("gamevault.connect")
      }
      strCancelButtonText={t("gamevault.cancel")}
      bOKDisabled={loading}
      onOK={handleConnect}
      onCancel={closeModal}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
        {/* ── Server URL ───────────────────────────────────────── */}
        <TextField
          label={t("gamevault.serverUrl")}
          value={serverUrl}
          onChange={(e) => setServerUrl(e.target.value)}
        />

        {/* ── Credentials ──────────────────────────────────────── */}
        <TextField
          label={t("gamevault.username")}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />

        <TextField
          label={t("gamevault.password")}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          bIsPassword
        />

        {/* ── TLS toggle ───────────────────────────────────────── */}
        <ToggleField
          label={t("gamevault.verifySsl")}
          description={t("gamevault.verifySslDescription")}
          checked={verifySsl}
          onChange={setVerifySsl}
        />

        {/* ── Temp download directory ──────────────────────────── */}
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
          <div style={{ flex: 1 }}>
            <TextField
              label={t("gamevault.downloadDir")}
              description={`${t("gamevault.downloadDirDescription")} (${t("gamevault.downloadDirPlaceholder")})`}
              value={downloadDir}
              onChange={(e) => setDownloadDir(e.target.value)}
            />
          </div>
          <DialogButton
            onClick={() => void pickDownloadDir()}
            style={{ minWidth: 48, width: 48, height: 40, padding: 0, flexShrink: 0 }}
          >
            📁
          </DialogButton>
        </div>

        {/* ── Error banner ─────────────────────────────────────── */}
        {error && (
          <div
            style={{
              color: "#ef4444",
              fontSize: "12px",
              padding: "4px 0",
            }}
          >
            {error}
          </div>
        )}
      </div>
    </ConfirmModal>
  );
};

export default GameVaultCredentialsModal;
