// Windows 10/11 include .NET Framework 4.x. No global Python or shell is used.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

[assembly: AssemblyTitle("COWMATA Annotator")]
[assembly: AssemblyDescription("Offline cattle video and IMU annotation workstation")]
[assembly: AssemblyCompany("COWMATA")]
[assembly: AssemblyVersion("3.1.0.0")]
[assembly: AssemblyFileVersion("3.1.0.0")]

internal static class Launcher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBox(IntPtr window, string text, string caption, uint flags);

    // Microsoft C argv quoting: preserve quotes, Unicode, and trailing slashes.
    internal static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(c);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string python = Path.Combine(root, "runtime", "pythonw.exe");
            string script = Path.Combine(root, "portable_start.py");
            foreach (string path in new [] { python, script,
                Path.Combine(root, "vendor", "vlc", "libvlc.dll"),
                Path.Combine(root, "vendor", "ffmpeg", "bin", "ffmpeg.exe") })
                if (!File.Exists(path)) throw new FileNotFoundException("软件包不完整，请重新安装或解压完整便携包。无需安装 Python。\n" + path);

            var command = new StringBuilder("-I ").Append(Quote(script));
            foreach (string argument in args) command.Append(' ').Append(Quote(argument));
            var start = new ProcessStartInfo(python, command.ToString());
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PYTHONPATH");
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            using (Process child = Process.Start(start))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox(IntPtr.Zero, error.Message, "COWMATA 启动失败", 0x10);
            return 1;
        }
    }
}
