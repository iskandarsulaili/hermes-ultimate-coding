case                       tool                   class              s  detail
rea_status                 rea_status             OK               1.9  
rea_engines                rea_engines            OK               0.0  
rea_capabilities           rea_capabilities       OK               3.0  
rea_providers              rea_providers          OK               3.0  
rea_commands               rea_commands           OK               0.0  
rea_doctor                 rea_doctor             OK               4.0  
rea_mcp_status             rea_mcp_status         OK               0.0  
rea_mcp_setup              rea_mcp_setup          OK               0.0  
rea_evidence               rea_evidence           OK               0.0  
rea_adb_devices            rea_adb_devices        OK               1.8  ev_6de0edf9339898a
rea_adb_inspect            rea_adb_inspect        OK               1.9  ev_f15f04b55c4b7e7
rea_adb_logcat             rea_adb_logcat         OK               1.8  ev_060e162a76e6e31
rea_adb_screen             rea_adb_screen         OK               2.1  ev_7c49f21dc994824
rea_adb_packages           rea_adb_packages       OK               1.7  ev_f4def2ff0646d0d
rea_adb_processes          rea_adb_processes      OK               2.0  ev_ff0a134305dc096
rea_adb_services           rea_adb_services       OK               2.1  ev_8ee359f9aae6caf
rea_adb_features           rea_adb_features       OK               1.9  ev_4b945cb5ca39b90
rea_adb_setting            rea_adb_setting        OK               1.8  ev_940651e9ed84b9e
rea_adb_directory          rea_adb_directory      OK               2.0  ev_0215ee70f8ef56b
rea_adb_transfer_push      rea_adb_transfer       OK               1.9  ev_bf0a26ea300942f
rea_adb_transfer_pull      rea_adb_transfer       OK               1.8  ev_1f8c6e9ab0ff650
rea_adb_bugreport          rea_adb_bugreport      OK              61.2  ev_7cb5eed92f503ea
rea_mobile_audit           rea_mobile_audit       OK               8.2  
rea_apk                    rea_apk                OK               6.3  ev_6da43863e923486
rea_apk_classes            rea_apk                OK               6.4  ev_91c87265772908e
rea_apk_resources          rea_apk                OK               5.6  ev_f6e9d2e32035a03
rea_apktool_client         rea_apktool_client     OK               1.9  ev_f7e9f4086da7f08
rea_jadx_client            rea_jadx_client        OK               6.2  ev_913dd21c1c8342a
rea_dart_flutter           rea_dart_flutter       OK               1.6  ev_24e2f5ed62f237f
rea_dart_aot               rea_dart_aot           OK               2.4  ev_04fdf3433e41108
rea_js_analyze             rea_js_analyze         OK              41.7  ev_1b5d6d69779a309
rea_js_recover             rea_js_recover         OK               3.1  ev_b8f9a048eae63da
rea_js_compare             rea_js_compare         OK               2.1  ev_c4b1a3d6cbf73b7
rea_native_overview        rea_native             OK              21.1  ev_b31dac381cb7d44
rea_native_function        rea_native             OK              20.6  ev_045dcaf907e60c1
rea_binary_meta            rea_binary_meta        OK               1.9  ev_531aa9873ab9429
rea_objdump                rea_objdump            OK               5.4  ev_0383c2c8a1510e4
rea_rizin                  rea_rizin              OK               9.8  ev_a2aea8c88213745
rea_gdb                    rea_gdb                OK               2.2  ev_30472cac29540d0
rea_artifact               rea_artifact           OK               2.3  ev_0433eb405fc1965
rea_compare                rea_compare            OK               1.6  
rea_sqlite                 rea_sqlite             OK               2.4  ev_24e699d71f7951f
rea_sqlite_table           rea_sqlite             OK               2.5  ev_073bf154932de44
rea_wasm                   rea_wasm               OK               5.1  ev_a247da987c09b13
rea_evm                    rea_evm                OK               2.6  ev_6d5b34a1ae0ac3d
rea_pcap                   rea_pcap               OK               5.7  ev_52c80e99f6238bd
rea_packets                rea_packets            OK               3.3  ev_ab43a693df2eab7
rea_firmware               rea_firmware           OK               2.8  ev_325d7fc24cf9db8
rea_process_capture        rea_process_capture    OK               5.7  ev_1d66ccfb66643c0
rea_verify_deploy          rea_verify_deploy      OK               2.1  
rea_web                    rea_web                OK               1.6  ev_bfe196d8398dc54
rea_signals                rea_signals            OK               2.9  ev_849953ebd2cb654
rea_frida_instrument       rea_frida_instrument   PRECONDITION     1.7  Analysis input is invalid. Check the arguments and try again
rea_screenshot_compare     rea_screenshot_compare OK               1.8  ev_7f16ac834ec8127
rea_run                    rea_run                OK               1.7  ev_6de0edf9339898a
rea_run_positional         rea_run                OK              15.8  ev_e29710ef76a790f
rea_run_options            rea_run                OK               2.6  ev_61d89e7d16191c4
rea_run_main_only          rea_run                OK               5.7  ev_52c80e99f6238bd
rea_run_unknown            rea_run                PRECONDITION     0.0  REA command 'definitely-not-a-real-command' is not in the in

CLASSES: {"OK": 57, "PRECONDITION": 2}
tools registered: 54 | cases run: 59
PRECONDITION: ['rea_frida_instrument', 'rea_run_unknown']
