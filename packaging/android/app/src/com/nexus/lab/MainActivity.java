package com.nexus.lab;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

/**
 * The NEXUS Android client.
 *
 * A native shell around the same dashboard the web serves: it is deliberately
 * NOT a second implementation of the product. Authentication is the web UI's
 * own login (a server-side session cookie), so the app never stores a PAT or an
 * LLM key. The only thing persisted locally is the backend URL.
 *
 * Server-side orchestration is unaffected by this app: closing it does not stop
 * a run, because the durable worker is a separate process on the server.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "nexus";
    private static final String KEY_URL = "backend_url";
    private static final String DEFAULT_URL = "http://10.0.2.2:8000"; // emulator -> host

    private WebView web;
    private LinearLayout setup;
    private TextView status;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xFF0B0D12);

        status = new TextView(this);
        status.setPadding(24, 24, 24, 8);
        status.setTextColor(0xFFE8EBF0);
        status.setVisibility(View.GONE);
        root.addView(status);

        setup = buildSetupPanel();
        root.addView(setup, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        CookieManager.getInstance().setAcceptCookie(true);
        web.setWebViewClient(new NexusWebViewClient());
        web.setVisibility(View.GONE);
        root.addView(web, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        setContentView(root);

        String url = getPrefs().getString(KEY_URL, null);
        if (url != null && !url.isEmpty()) {
            load(url);
        } else {
            handleDeepLink(getIntent());
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleDeepLink(intent);
    }

    private void handleDeepLink(Intent intent) {
        if (intent != null && Intent.ACTION_VIEW.equals(intent.getAction())
                && intent.getData() != null && "nexus".equals(intent.getData().getScheme())) {
            String target = intent.getData().getQueryParameter("url");
            if (target != null && (target.startsWith("http://") || target.startsWith("https://"))) {
                getPrefs().edit().putString(KEY_URL, target).apply();
                load(target);
                return;
            }
        }
        if (getPrefs().getString(KEY_URL, null) == null) {
            showSetup();
        }
    }

    private LinearLayout buildSetupPanel() {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(32, 48, 32, 32);

        TextView title = new TextView(this);
        title.setText("NEXUS");
        title.setTextSize(28);
        title.setTextColor(0xFFE8EBF0);
        panel.addView(title);

        TextView hint = new TextView(this);
        hint.setText(getString(R.string.backend_hint));
        hint.setPadding(0, 16, 0, 8);
        hint.setTextColor(0xFF9AA3B2);
        panel.addView(hint);

        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("https://nexus.example.com");
        input.setTextColor(0xFFE8EBF0);
        input.setHintTextColor(0xFF6B7280);
        input.setText(getPrefs().getString(KEY_URL, ""));
        panel.addView(input);

        Button connect = new Button(this);
        connect.setText(getString(R.string.connect));
        connect.setOnClickListener(v -> {
            String url = input.getText().toString().trim();
            if (!url.startsWith("http://") && !url.startsWith("https://")) {
                Toast.makeText(this, "URL must start with http:// or https://",
                        Toast.LENGTH_SHORT).show();
                return;
            }
            getPrefs().edit().putString(KEY_URL, url).apply();
            load(url);
        });
        panel.addView(connect);

        TextView note = new TextView(this);
        note.setText("The app stores only this URL. Sign-in happens on the server "
                + "and no GitHub token or LLM key is kept on the device.");
        note.setPadding(0, 24, 0, 0);
        note.setTextColor(0xFF9AA3B2);
        panel.addView(note);
        return panel;
    }

    private SharedPreferences getPrefs() {
        return getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private void showSetup() {
        setup.setVisibility(View.VISIBLE);
        web.setVisibility(View.GONE);
        status.setVisibility(View.GONE);
    }

    private void load(String url) {
        setup.setVisibility(View.GONE);
        status.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        web.loadUrl(url);
    }

    private void showOffline() {
        status.setText(getString(R.string.offline));
        status.setVisibility(View.VISIBLE);
    }

    @Override
    public void onBackPressed() {
        if (web.getVisibility() == View.VISIBLE && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    private class NexusWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            String scheme = uri.getScheme();
            if ("http".equals(scheme) || "https".equals(scheme)) {
                return false; // keep NEXUS navigation inside the app
            }
            // Anything else (mailto:, tel:, intent:) goes to the system.
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, uri));
            } catch (Exception ignored) {
                // No handler installed; nothing we can do.
            }
            return true;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            status.setVisibility(View.GONE);
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) {
                showOffline();
            }
        }
    }
}
