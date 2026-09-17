package uk.aive.imnotahuman

import android.annotation.SuppressLint
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import uk.aive.imnotahuman.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private var loaded = false
    private var lastBack = 0L
    private val webUrl = "https://imnotahuman.ai-ve.uk"
    private val host = "imnotahuman.ai-ve.uk"

    override fun onCreate(savedInstanceState: Bundle?) {
        val splash = installSplashScreen()
        splash.setKeepOnScreenCondition { !loaded }
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.parseColor("#090B0C")
        WindowInsetsControllerCompat(window, window.decorView).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, ime.bottom))
            insets
        }
        setupWebView()
        setupBack()
        binding.btnRetry.setOnClickListener { if (online()) load(webUrl) else showError() }
        val restored = savedInstanceState != null && binding.webView.restoreState(savedInstanceState) != null
        if (restored) loaded = true else if (online()) load(launchUrl(intent)) else showError()
    }

    private fun launchUrl(incoming: Intent?): String {
        val uri = incoming?.data ?: return webUrl
        return if (uri.scheme == "https" && uri.host == host) uri.toString() else webUrl
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        load(launchUrl(intent))
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() = with(binding.webView) {
        setBackgroundColor(Color.parseColor("#090B0C"))
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.cacheMode = WebSettings.LOAD_DEFAULT
        settings.setSupportZoom(false)
        settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        settings.userAgentString = "${settings.userAgentString} ImNotAHumanApp/1.2"
        CookieManager.getInstance().setAcceptCookie(true)
        webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, progress: Int) {
                binding.progressBar.progress = progress
                binding.progressBar.visibility = if (progress < 100) View.VISIBLE else View.GONE
            }
        }
        webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, icon: Bitmap?) {
                binding.progressBar.visibility = View.VISIBLE
            }
            override fun onPageFinished(view: WebView?, url: String?) {
                loaded = true
                binding.progressBar.visibility = View.GONE
                CookieManager.getInstance().flush()
            }
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val url = request?.url ?: return false
                if (url.scheme == "https" && url.host == host) return false
                if (url.scheme in setOf("https", "http", "mailto")) {
                    runCatching { startActivity(Intent(Intent.ACTION_VIEW, url)) }
                        .onFailure { Toast.makeText(this@MainActivity, "연결할 앱을 찾을 수 없습니다", Toast.LENGTH_SHORT).show() }
                }
                return true
            }
            override fun onReceivedError(view: WebView?, request: WebResourceRequest?, error: WebResourceError?) {
                if (request?.isForMainFrame == true) showError()
            }
        }
        isVerticalScrollBarEnabled = false
        overScrollMode = View.OVER_SCROLL_NEVER
    }

    private fun setupBack() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                binding.webView.evaluateJavascript(
                    """
                    (() => {
                      for (const id of ['infoModal', 'duoModal', 'exitModal']) {
                        const modal = document.getElementById(id);
                        if (modal && !modal.hidden) { modal.hidden = true; return 'handled'; }
                      }
                      const ending = document.getElementById('endingModal');
                      if (ending && !ending.hidden) { document.getElementById('endingLeave')?.click(); return 'handled'; }
                      const ranking = document.getElementById('rankingScreen');
                      if (ranking && !ranking.hidden) { document.getElementById('closeRanking')?.click(); return 'handled'; }
                      const game = document.getElementById('gameScreen');
                      const waiting = document.getElementById('waitingScreen');
                      if (game && !game.hidden) { document.getElementById('exitGame')?.click(); return 'handled'; }
                      if (waiting && !waiting.hidden) { document.getElementById('leaveRoom')?.click(); return 'handled'; }
                      return 'unhandled';
                    })()
                    """.trimIndent()
                ) { result -> if (result != "\"handled\"") handleRegularBack() }
            }
        })
    }

    private fun handleRegularBack() {
        if (binding.webView.canGoBack()) { binding.webView.goBack(); return }
        val now = System.currentTimeMillis()
        if (now - lastBack < 2000) { finish(); return }
        lastBack = now
        Toast.makeText(this, "한 번 더 누르면 종료됩니다", Toast.LENGTH_SHORT).show()
    }

    private fun load(url: String) {
        binding.errorContainer.visibility = View.GONE
        binding.webView.visibility = View.VISIBLE
        binding.webView.loadUrl(url)
    }
    private fun showError() {
        loaded = true
        binding.webView.visibility = View.GONE
        binding.errorContainer.visibility = View.VISIBLE
        binding.progressBar.visibility = View.GONE
    }
    private fun online(): Boolean {
        val manager = getSystemService(ConnectivityManager::class.java)
        val network = manager.activeNetwork ?: return false
        return manager.getNetworkCapabilities(network)?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) == true
    }
    override fun onSaveInstanceState(outState: Bundle) {
        binding.webView.saveState(outState)
        super.onSaveInstanceState(outState)
    }
}
