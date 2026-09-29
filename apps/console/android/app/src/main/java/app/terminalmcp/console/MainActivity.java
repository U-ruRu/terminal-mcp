package app.terminalmcp.console;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(TerminalApkInstallerPlugin.class);
        super.onCreate(savedInstanceState);
    }
}
