package app.terminalmcp.console;

import android.content.Intent;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.Uri;
import android.os.Build;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;
import java.util.Locale;

@CapacitorPlugin(name = "TerminalApkInstaller")
public class TerminalApkInstallerPlugin extends Plugin {
    private static final int BUFFER_SIZE = 64 * 1024;

    @PluginMethod
    public void getInstalledInfo(PluginCall call) {
        try {
            PackageInfo info = packageInfo(getContext().getPackageName(), false);
            JSObject result = new JSObject();
            result.put("versionName", info.versionName == null ? "" : info.versionName);
            result.put("versionCode", versionCode(info));
            result.put("signingCertificateSha256", certificateSha256(info));
            call.resolve(result);
        } catch (Exception error) {
            call.reject("installed_info_failed", error);
        }
    }

    @PluginMethod
    public void downloadAndInstall(PluginCall call) {
        final String url = call.getString("url");
        final String expectedSha = normalizeSha256(call.getString("sha256"));
        final String expectedCert = normalizeSha256(call.getString("signingCertificateSha256"));
        final long expectedSize = call.getData().optLong("size", -1L);

        if (url == null || expectedSha == null || expectedCert == null || expectedSize < 1) {
            call.reject("invalid_update_request");
            return;
        }

        new Thread(() -> {
            try {
                URL source = new URL(url);
                if (!"https".equalsIgnoreCase(source.getProtocol())) {
                    throw new IllegalArgumentException("insecure_apk_url");
                }

                File updateDir = new File(getContext().getCacheDir(), "updates");
                if (!updateDir.exists() && !updateDir.mkdirs()) {
                    throw new IllegalStateException("update_cache_unavailable");
                }
                File apk = new File(updateDir, "terminal-mcp-update.apk");
                downloadVerified(source, apk, expectedSize, expectedSha);
                verifyArchive(apk, expectedCert);

                Uri uri = FileProvider.getUriForFile(
                    getContext(),
                    getContext().getPackageName() + ".fileprovider",
                    apk
                );
                Intent intent = new Intent(Intent.ACTION_VIEW);
                intent.setDataAndType(uri, "application/vnd.android.package-archive");
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                getContext().startActivity(intent);

                JSObject result = new JSObject();
                result.put("opened", true);
                call.resolve(result);
            } catch (Exception error) {
                call.reject(error.getMessage() == null ? "install_failed" : error.getMessage(), error);
            }
        }, "terminal-mcp-apk-installer").start();
    }

    private void downloadVerified(
        URL source,
        File destination,
        long expectedSize,
        String expectedSha
    ) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) source.openConnection();
        connection.setConnectTimeout(15_000);
        connection.setReadTimeout(30_000);
        connection.setInstanceFollowRedirects(true);
        connection.setRequestProperty("Accept", "application/vnd.android.package-archive");
        connection.connect();

        if (connection.getResponseCode() < 200 || connection.getResponseCode() >= 300) {
            throw new IllegalStateException("apk_download_failed");
        }
        if (!"https".equalsIgnoreCase(connection.getURL().getProtocol())) {
            throw new IllegalStateException("insecure_apk_redirect");
        }
        long contentLength = connection.getContentLengthLong();
        if (contentLength >= 0 && contentLength != expectedSize) {
            throw new IllegalStateException("apk_size_mismatch");
        }

        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        long total = 0L;
        try (
            BufferedInputStream input = new BufferedInputStream(connection.getInputStream());
            FileOutputStream output = new FileOutputStream(destination, false)
        ) {
            byte[] buffer = new byte[BUFFER_SIZE];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                total += read;
                if (total > expectedSize) {
                    throw new IllegalStateException("apk_size_mismatch");
                }
                digest.update(buffer, 0, read);
                output.write(buffer, 0, read);
            }
            output.getFD().sync();
        } finally {
            connection.disconnect();
        }

        if (total != expectedSize) {
            throw new IllegalStateException("apk_size_mismatch");
        }
        String actualSha = hex(digest.digest());
        if (!actualSha.equals(expectedSha)) {
            destination.delete();
            throw new IllegalStateException("apk_sha256_mismatch");
        }
    }

    private void verifyArchive(File apk, String expectedCert) throws Exception {
        PackageInfo archive = packageInfo(apk.getAbsolutePath(), true);
        if (archive == null || !getContext().getPackageName().equals(archive.packageName)) {
            throw new IllegalStateException("apk_package_mismatch");
        }
        if (!certificateSha256(archive).equals(expectedCert)) {
            throw new IllegalStateException("apk_signing_certificate_mismatch");
        }

        PackageInfo installed = packageInfo(getContext().getPackageName(), false);
        if (!certificateSha256(installed).equals(expectedCert)) {
            throw new IllegalStateException("installed_signing_certificate_mismatch");
        }
        if (versionCode(archive) <= versionCode(installed)) {
            throw new IllegalStateException("apk_version_not_newer");
        }
    }

    @SuppressWarnings("deprecation")
    private PackageInfo packageInfo(String value, boolean archive) throws Exception {
        PackageManager manager = getContext().getPackageManager();
        int flags = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
            ? PackageManager.GET_SIGNING_CERTIFICATES
            : PackageManager.GET_SIGNATURES;
        PackageInfo result = archive
            ? manager.getPackageArchiveInfo(value, flags)
            : manager.getPackageInfo(value, flags);
        if (result == null) {
            throw new IllegalStateException("apk_metadata_unavailable");
        }
        return result;
    }

    @SuppressWarnings("deprecation")
    private String certificateSha256(PackageInfo info) throws Exception {
        Signature[] signatures;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            if (info.signingInfo == null) {
                throw new IllegalStateException("signing_certificate_unavailable");
            }
            signatures = info.signingInfo.getApkContentsSigners();
        } else {
            signatures = info.signatures;
        }
        if (signatures == null || signatures.length != 1) {
            throw new IllegalStateException("unexpected_signing_certificate_count");
        }
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        return hex(digest.digest(signatures[0].toByteArray()));
    }

    @SuppressWarnings("deprecation")
    private long versionCode(PackageInfo info) {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
            ? info.getLongVersionCode()
            : info.versionCode;
    }

    private static String normalizeSha256(String value) {
        if (value == null) return null;
        String normalized = value.replace(":", "").trim().toLowerCase(Locale.ROOT);
        return normalized.matches("[a-f0-9]{64}") ? normalized : null;
    }

    private static String hex(byte[] bytes) {
        StringBuilder builder = new StringBuilder(bytes.length * 2);
        for (byte item : bytes) {
            builder.append(String.format(Locale.ROOT, "%02x", item & 0xff));
        }
        return builder.toString();
    }
}
