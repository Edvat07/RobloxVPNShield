# 🛡️ Roblox Shield (v2)

Chỉ cho **Roblox** đi qua đường hầm WireGuard (Proton VPN), mọi app khác vẫn dùng mạng Việt Nam bình thường.

- **Không rò IP:** Roblox không có đường dự phòng — VPN rớt thì Roblox mất mạng chứ không lộ IP Việt Nam.
- **Kill switch:** khi bảo vệ chưa bật, mọi tiến trình Roblox bị đóng ngay. Tắt/crash app thì Roblox và sing-box cũng tắt theo (Job Object).
- **Kiểm tra IP ra** lúc kết nối và mỗi phút một lần.
- Không cần mở app Proton VPN — app tự chạy WireGuard bằng `sing-box`.

## Cách dùng
1. Tải file cấu hình WireGuard (`.conf`) từ trang tài khoản Proton VPN (Downloads → WireGuard configuration).
2. Mở `v2\release\RobloxShield.exe` (hoặc shortcut **Roblox Shield** ngoài Desktop), chấp nhận quyền Admin.
3. Bấm **Chọn file .conf…** (chỉ lần đầu).
4. Bấm **Bật bảo vệ & vào game**.

> Đừng bấm Connect trong app Proton VPN cùng lúc — hai VPN sẽ tranh nhau. App sẽ báo nếu phát hiện VPN khác đang bật.

## 🎮 Steam Unblock
Nhà mạng chặn Steam theo 2 lớp: DNS bị giả mạo (kể cả khi đổi sang 8.8.8.8) và DPI ngắt kết nối TLS khi đọc thấy tên miền Steam.
`SteamUnblock.exe` vượt cả hai **mà không dùng VPN**:

- Tên miền Steam được phân giải qua DNS-over-HTTPS (1.1.1.1) và nhận IP ảo (fake-IP).
- Chỉ dải IP ảo đó đi vào card mạng ảo. Ở đó gói ClientHello được chia nhỏ thành nhiều TLS record để DPI không đọc được, rồi kết nối **đi thẳng** ra mạng nhà.
- Game, voice và mọi app khác **không đi qua** sing-box → ping không đổi.

Cách dùng: mở shortcut **Steam Unblock** (chấp nhận quyền Admin), đợi "Đã mở khoá Steam ✓", rồi bấm **Mở Steam**. Nếu Steam đã mở sẵn mà báo lỗi mạng, thoát hẳn Steam rồi mở lại.
Bấm X hoặc thu nhỏ cửa sổ thì app **chạy ngầm ở khay hệ thống** (góc phải thanh Taskbar) và Steam vẫn được mở khoá. Bấm vào biểu tượng để mở lại cửa sổ, chuột phải → **Thoát** để tắt hẳn.

Roblox Shield cũng tự mở khoá Steam theo cách tương tự. Không chạy hai app cùng lúc: Steam Unblock sẽ tự nhường nếu thấy Roblox Shield đang chạy.

## Build
Chạy `v2\build.bat` (cần Python 3.12). Kết quả (`RobloxShield.exe`, `SteamUnblock.exe`) nằm ở `v2\release\`, `sing-box.exe` và `wintun.dll` được chép từ `core\`.
