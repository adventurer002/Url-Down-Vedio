import { Link } from "react-router-dom";
export default function InfoView({ kind }: { kind: "help" | "privacy" }) {
  const help = kind === "help";
  return (
    <div className="preferences-page">
      <Link className="back-link" to="/">
        ← 返回首页
      </Link>
      <h1>{help ? "使用帮助" : "隐私说明"}</h1>
      {help ? (
        <>
          <section className="preferences-card">
            <h2>从一个视频链接开始</h2>
            <p>
              粘贴公开、免登录的视频链接，点击“开始解析”。解析完成后，可选择视频下载、音频提取、字幕转写、AI
              总结或思维导图。
            </p>
          </section>
          <section className="preferences-card">
            <h2>查看进度和历史</h2>
            <p>
              在顶部“任务历史”中查看任务进度和结果。需要登录时，关注公众号并发送弹窗中的六位口令。登录后的历史列表仅展示当前账户的任务。
            </p>
          </section>
          <section className="preferences-card">
            <h2>管理下载和存储</h2>
            <p>
              任务完成后可保存文件。下载文件可能在过期后被清理；请及时保存需要保留的内容。过期缓存清理入口位于
              <Link to="/settings">设置</Link>。
            </p>
          </section>
        </>
      ) : (
        <>
          <section className="preferences-card">
            <h2>账户与任务记录</h2>
            <p>
              使用公众号登录时，服务会记录微信用户标识、昵称、关注状态和登录时间。视频链接、任务记录及处理结果保存在当前服务中。
            </p>
          </section>
          <section className="preferences-card">
            <h2>浏览器中的数据</h2>
            <p>
              浏览器会保存登录
              Cookie、登录凭证及工作台记录，以恢复登录和任务进度。退出会清除当前登录凭证。已下载的文件由你自行保管。
            </p>
          </section>
          <section className="preferences-card">
            <h2>内容处理</h2>
            <p>
              视频解析会访问对应的视频平台。启用云端语音识别或 AI
              服务时，所需音频或文稿会发送至部署者配置的服务提供方。文件与凭证的保留期限由服务配置决定。
            </p>
          </section>
        </>
      )}
    </div>
  );
}
