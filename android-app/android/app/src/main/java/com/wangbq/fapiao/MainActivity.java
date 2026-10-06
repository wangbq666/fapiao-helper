package com.wangbq.fapiao;

import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        handleShare(getIntent());
    }

    @Override
    public void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleShare(intent);
    }

    @Override
    public void onResume() {
        super.onResume();
        // 页面就绪后让它来认领缓存里的分享文件
        notifyWebView();
    }

    private void handleShare(Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        Uri uri = null;
        if (Intent.ACTION_SEND.equals(action)) {
            uri = intent.getParcelableExtra(Intent.EXTRA_STREAM);
        } else if (Intent.ACTION_VIEW.equals(action)) {
            uri = intent.getData();
        }
        if (uri == null) return;
        try {
            InputStream in = getContentResolver().openInputStream(uri);
            if (in == null) return;
            File out = new File(getCacheDir(), "shared-latest.pdf");
            OutputStream os = new FileOutputStream(out);
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
            os.close();
            in.close();
        } catch (Exception ignored) {
        }
    }

    private void notifyWebView() {
        try {
            android.webkit.WebView wv = bridge.getWebView();
            if (wv == null) return;
            wv.evaluateJavascript(
                "window.__checkNativeShare && window.__checkNativeShare();", null);
        } catch (Exception ignored) {
            // WebView 还没就绪; JS 启动时会自己查缓存
        }
    }
}
