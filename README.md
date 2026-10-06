# AGY Delegator

Giao AGY CLI đọc và sửa file, tiếp tục trong cùng phiên, và xem tiến trình qua
dashboard AGY Live. Ứng dụng điều phối giữ phần chạy lệnh và kiểm tra. Dashboard
chỉ chạy trên máy của bạn và không tự mở trình duyệt.

## Bắt đầu

Cần macOS hoặc Linux, Python 3.10+, AGY CLI đã cài và đăng nhập. Clone repo rồi
xem trước vị trí cài skill cho project:

```sh
git clone https://github.com/hieutran0413/agy-delegator.git
cd agy-delegator
python3 tools/setup_client.py --app cursor --project /duong/dan/toi/project
```

Đổi `cursor` thành `codex`, `claude` hoặc `antigravity`. Khi vị trí đúng, thêm
`--apply` để cài skill. Công cụ không ghi đè cấu hình ứng dụng; nó in cấu hình
MCP để bạn thêm theo hướng dẫn của ứng dụng. Mở lại ứng dụng để nạp skill.

## Dùng

Gọi skill `delegate-to-agy`, rồi giao việc đọc hoặc sửa file. Mở dashboard tại
`http://127.0.0.1:8775/` nếu muốn xem hoạt động, đọc kết quả đầy đủ, dừng việc,
hoặc nhắn tiếp trong cùng phiên. Dữ liệu phiên được giữ cục bộ tại
`~/.agy-delegator/live`.

Codex hoặc ứng dụng điều phối chạy lệnh và kiểm tra sau khi AGY sửa xong. Có thể
cấu hình các thao tác dùng chung qua MCP để AGY yêu cầu điều phối chạy một thao
tác đã đăng ký. Đây là lớp chặn qua hook, không phải cách ly ở cấp hệ điều hành;
chỉ đăng ký thao tác bạn tin cậy.

## Giới hạn

Hỗ trợ macOS/Linux; Windows chưa hỗ trợ. Skill và dashboard đã thử trên máy phát
triển; các ứng dụng khác và Linux cần kiểm tra trong môi trường sử dụng thực tế.
AGY có thể nhìn thấy nhiều công cụ hơn giới hạn mong muốn trong một số chế độ,
nên quy tắc file-only vẫn dựa một phần vào hướng dẫn cho worker.

Dashboard đã build sẵn. Để build lại, cài Node.js rồi chạy `npm ci` và
`npm run build` trong thư mục `dashboard`. Thông báo giấy phép thư viện nằm trong
`THIRD_PARTY_LICENSES`.

Chi tiết quy trình nằm trong `skills/delegate-to-agy/SKILL.md`.
