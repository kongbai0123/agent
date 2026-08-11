using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Local AI Workbench")]
[assembly: AssemblyDescription("Local AI Workbench bootstrap launcher")]
[assembly: AssemblyCompany("Local AI Workbench")]
[assembly: AssemblyProduct("Local AI Workbench")]
[assembly: AssemblyCopyright("Copyright (c) 2026")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

namespace LocalAIWorkbenchLauncher
{
    internal static class Program
    {
        private const int MinimumPort = 1024;
        private const int MaximumPort = 65535;

        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                string projectRoot = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(
                    Path.DirectorySeparatorChar,
                    Path.AltDirectorySeparatorChar
                );
                string launchScript = Path.Combine(projectRoot, "scripts", "launch_workbench.ps1");
                string powershell = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                    "System32",
                    "WindowsPowerShell",
                    "v1.0",
                    "powershell.exe"
                );

                if (!File.Exists(launchScript))
                {
                    return ShowError(
                        "The launcher script is missing:\n\n" + launchScript +
                        "\n\nKeep LocalAIWorkbench.exe inside the complete project folder."
                    );
                }
                if (!File.Exists(powershell))
                {
                    return ShowError("Windows PowerShell could not be found.");
                }

                bool shouldWait;
                IList<string> forwarded = ParseArguments(args, out shouldWait);
                string arguments =
                    "-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " +
                    QuoteArgument(launchScript);
                foreach (string item in forwarded)
                {
                    arguments += " " + QuoteArgument(item);
                }

                ProcessStartInfo startInfo = new ProcessStartInfo();
                startInfo.FileName = powershell;
                startInfo.Arguments = arguments;
                startInfo.WorkingDirectory = projectRoot;
                startInfo.UseShellExecute = false;
                startInfo.CreateNoWindow = true;
                startInfo.WindowStyle = ProcessWindowStyle.Hidden;

                using (Process process = Process.Start(startInfo))
                {
                    if (process == null)
                    {
                        return ShowError("Windows could not start the Workbench launcher.");
                    }
                    if (shouldWait)
                    {
                        process.WaitForExit();
                        return process.ExitCode;
                    }
                }
                return 0;
            }
            catch (ArgumentException exception)
            {
                return ShowError(exception.Message);
            }
            catch (Exception exception)
            {
                return ShowError("Local AI Workbench could not start.\n\n" + exception.Message);
            }
        }

        private static IList<string> ParseArguments(string[] args, out bool shouldWait)
        {
            List<string> forwarded = new List<string>();
            shouldWait = false;

            for (int index = 0; index < args.Length; index++)
            {
                string value = args[index];
                switch (value.ToLowerInvariant())
                {
                    case "--skip-update":
                    case "--no-update":
                        forwarded.Add("-SkipUpdate");
                        break;
                    case "--smoke-test":
                        forwarded.Add("-SmokeTest");
                        shouldWait = true;
                        break;
                    case "--no-browser":
                        forwarded.Add("-NoBrowser");
                        break;
                    case "--wait":
                        shouldWait = true;
                        break;
                    case "--backend-port":
                        forwarded.Add("-BackendPort");
                        forwarded.Add(ParsePort(args, ref index, value));
                        break;
                    case "--frontend-port":
                        forwarded.Add("-FrontendPort");
                        forwarded.Add(ParsePort(args, ref index, value));
                        break;
                    default:
                        throw new ArgumentException(
                            "Unsupported launcher option: " + value +
                            "\n\nAllowed options: --skip-update, --smoke-test, --no-browser, " +
                            "--wait, --backend-port, --frontend-port."
                        );
                }
            }
            return forwarded;
        }

        private static string ParsePort(string[] args, ref int index, string option)
        {
            if (index + 1 >= args.Length)
            {
                throw new ArgumentException(option + " requires a port number.");
            }
            index++;
            int port;
            if (!Int32.TryParse(args[index], NumberStyles.None, CultureInfo.InvariantCulture, out port) ||
                port < MinimumPort || port > MaximumPort)
            {
                throw new ArgumentException(option + " must be between 1024 and 65535.");
            }
            return port.ToString(CultureInfo.InvariantCulture);
        }

        private static string QuoteArgument(string value)
        {
            return "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private static int ShowError(string message)
        {
            MessageBox.Show(
                message,
                "Local AI Workbench",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return 2;
        }
    }
}
