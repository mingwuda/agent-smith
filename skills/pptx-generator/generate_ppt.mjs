import PptxGenJS from "pptxgenjs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { mkdirSync } from "fs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// ─── 内置主题 ───────────────────────────────────────────────
const THEMES = {
  Corporate: {
    name: "Corporate",
    primary: "#1B365D",
    secondary: "#4A90D9",
    bg: "#FFFFFF",
    text: "#333333",
    lightText: "#FFFFFF",
    desc: "商务汇报、企业演示",
  },
  Creative: {
    name: "Creative",
    primary: "#E67E22",
    secondary: "#2C3E50",
    bg: "#FFFFFF",
    text: "#333333",
    lightText: "#FFFFFF",
    desc: "创意提案、营销方案",
  },
  Minimal: {
    name: "Minimal",
    primary: "#000000",
    secondary: "#666666",
    bg: "#FFFFFF",
    text: "#000000",
    lightText: "#FFFFFF",
    desc: "简约风格、学术报告",
  },
  Nature: {
    name: "Nature",
    primary: "#27AE60",
    secondary: "#2ECC71",
    bg: "#F5F5DC",
    text: "#333333",
    lightText: "#FFFFFF",
    desc: "环保、健康、教育",
  },
  Tech: {
    name: "Tech",
    primary: "#8E44AD",
    secondary: "#1ABC9C",
    bg: "#FFFFFF",
    text: "#333333",
    lightText: "#FFFFFF",
    desc: "科技、互联网、产品发布",
  },
  Elegant: {
    name: "Elegant",
    primary: "#D4AF37",
    secondary: "#34495E",
    bg: "#FFFFFF",
    text: "#333333",
    lightText: "#FFFFFF",
    desc: "高端品牌、颁奖典礼",
  },
};

// ─── 参数解析 ───────────────────────────────────────────────
function parseArgs(argv) {
  const args = {
    title: "演示文稿",
    theme: "Corporate",
    output: "output.pptx",
    slides: [],
  };

  for (let i = 2; i < argv.length; i++) {
    switch (argv[i]) {
      case "--title":
        args.title = argv[++i];
        break;
      case "--theme":
        args.theme = argv[++i];
        break;
      case "--output":
        args.output = argv[++i];
        break;
      case "--slides":
        args.slides.push(JSON.parse(argv[++i]));
        break;
      case "--list-themes":
        printThemes();
        process.exit(0);
        break;
      case "--help":
        printHelp();
        process.exit(0);
        break;
    }
  }
  return args;
}

function printThemes() {
  console.log("\n📋 可用主题：\n");
  console.log(
    "┌───────────┬──────────────┬──────────────┬──────────────────────┐"
  );
  console.log(
    "│ 主题名称   │ 主色         │ 辅色         │ 适用场景              │"
  );
  console.log(
    "├───────────┼──────────────┼──────────────┼──────────────────────┤"
  );
  for (const [key, theme] of Object.entries(THEMES)) {
    console.log(
      `│ ${key.padEnd(9)}│ ${theme.primary}  ${theme.secondary}  │ ${theme.desc.padEnd(16)}│`
    );
  }
  console.log(
    "└───────────┴──────────────┴──────────────┴──────────────────────┘\n"
  );
}

function printHelp() {
  console.log(`
📊 PPT 生成器 - 使用帮助

用法: node generate_ppt.mjs [选项]

选项:
  --title <标题>        PPT 标题（默认: "演示文稿"）
  --theme <主题名>      选择主题（默认: Corporate）
                        可用主题: ${Object.keys(THEMES).join(", ")}
  --output <路径>       输出文件路径（默认: output.pptx）
  --slides <JSON>       幻灯片内容（可多次指定）
  --list-themes         列出所有可用主题
  --help                显示此帮助信息

幻灯片类型:

1. 标题页:
   {"type":"title","title":"主标题","subtitle":"副标题"}

2. 内容页:
   {"type":"content","title":"标题","bullets":["要点1","要点2","要点3"]}

3. 图文页:
   {"type":"image","title":"标题","imageUrl":"图片URL"}

4. 表格页:
   {"type":"table","title":"标题","headers":["列1","列2"],"rows":[["行1列1","行1列2"]]}

5. 图表页:
   {"type":"chart","title":"标题","chartType":"bar|line|pie","categories":["类别1"],"values":[100]}

示例:
  node generate_ppt.mjs \\
    --title "2024年度总结" \\
    --theme Corporate \\
    --output "reports/pptx/summary.pptx" \\
    --slides '{"type":"title","title":"2024年度总结","subtitle":"汇报人：张三"}' \\
    --slides '{"type":"content","title":"工作成果","bullets":["完成项目A","完成项目B"]}'
`);
}

// ─── 幻灯片生成 ─────────────────────────────────────────────
function addTitleSlide(prs, slideData, theme) {
  const slide = prs.addSlide();
  slide.background = { color: theme.bg };

  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: "100%",
    h: 1.2,
    fill: { color: theme.primary },
  });

  slide.addText(slideData.title || "演示文稿", {
    x: 1,
    y: 1.8,
    w: "80%",
    fontSize: 44,
    bold: true,
    color: theme.primary,
    fontFace: "Microsoft YaHei",
    align: "center",
  });

  if (slideData.subtitle) {
    slide.addText(slideData.subtitle, {
      x: 1,
      y: 3.2,
      w: "80%",
      fontSize: 20,
      color: theme.secondary,
      fontFace: "Microsoft YaHei",
      align: "center",
    });
  }

  slide.addShape("line", {
    x1: "10%",
    y1: 4.5,
    x2: "90%",
    y2: 4.5,
    line: { color: theme.secondary, width: 2 },
  });
}

function addContentSlide(prs, slideData, theme) {
  const slide = prs.addSlide();
  slide.background = { color: theme.bg };

  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: "100%",
    h: 0.8,
    fill: { color: theme.primary },
  });

  slide.addText(slideData.title || "", {
    x: 0.5,
    y: 0.3,
    w: "90%",
    fontSize: 28,
    bold: true,
    color: theme.lightText,
    fontFace: "Microsoft YaHei",
  });

  if (slideData.bullets && slideData.bullets.length > 0) {
    const bulletText = slideData.bullets.map(b => ({
      text: b,
      options: {
        bullet: { type: "symbol", char: "●", color: theme.secondary, space: 0.3 },
        fontSize: 18,
        color: theme.text,
        fontFace: "Microsoft YaHei",
        lineSpacingMultiple: 1.8,
      }
    }));
    slide.addText(bulletText, {
      x: 0.8,
      y: 1.5,
      w: "85%",
      h: "70%",
      valign: "top",
    });
  }
}

function addImageSlide(prs, slideData, theme) {
  const slide = prs.addSlide();
  slide.background = { color: theme.bg };

  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: "100%",
    h: 0.8,
    fill: { color: theme.primary },
  });

  slide.addText(slideData.title || "", {
    x: 0.5,
    y: 0.3,
    w: "90%",
    fontSize: 28,
    bold: true,
    color: theme.lightText,
    fontFace: "Microsoft YaHei",
  });

  if (slideData.imageUrl) {
    slide.addImage({
      path: slideData.imageUrl,
      x: 0.5,
      y: 1.5,
      w: "90%",
      h: 4.5,
    });
  } else {
    slide.addText("📷 图片占位", {
      x: 0.5,
      y: 1.5,
      w: "90%",
      h: 4.5,
      fontSize: 24,
      color: "#999999",
      fontFace: "Microsoft YaHei",
      align: "center",
      valign: "middle",
    });
  }
}

function addTableSlide(prs, slideData, theme) {
  const slide = prs.addSlide();
  slide.background = { color: theme.bg };

  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: "100%",
    h: 0.8,
    fill: { color: theme.primary },
  });

  slide.addText(slideData.title || "", {
    x: 0.5,
    y: 0.3,
    w: "90%",
    fontSize: 28,
    bold: true,
    color: theme.lightText,
    fontFace: "Microsoft YaHei",
  });

  if (slideData.headers && slideData.rows) {
    const tableData = [slideData.headers, ...slideData.rows];
    slide.addTable(tableData, {
      x: 0.5,
      y: 1.5,
      w: "90%",
      colW: Array(slideData.headers.length).fill(
        `=${9 / slideData.headers.length}`
      ),
      border: {
        type: "single",
        pt: 1,
        color: "#CCCCCC",
      },
      rowH: 0.5,
      fill: {
        type: "solid",
        color: theme.bg,
      },
      fontSize: 14,
      fontFace: "Microsoft YaHei",
      color: theme.text,
      rows: [
        {
          fill: { color: theme.primary },
          color: theme.lightText,
          bold: true,
          fontSize: 16,
          fontFace: "Microsoft YaHei",
        },
      ],
    });
  }
}

function addChartSlide(prs, slideData, theme) {
  const slide = prs.addSlide();
  slide.background = { color: theme.bg };

  slide.addShape("rect", {
    x: 0,
    y: 0,
    w: "100%",
    h: 0.8,
    fill: { color: theme.primary },
  });

  slide.addText(slideData.title || "", {
    x: 0.5,
    y: 0.3,
    w: "90%",
    fontSize: 28,
    bold: true,
    color: theme.lightText,
    fontFace: "Microsoft YaHei",
  });

  if (slideData.chartType && slideData.categories && slideData.values) {
    const chartType = slideData.chartType === "pie" ? "pie" : "barColClustered";
    const chartData = [{
      values: slideData.values,
      labels: slideData.categories,
      name: slideData.title || "图表"
    }];
    slide.addChart(chartType, chartData, {
      x: 0.5,
      y: 1.5,
      w: "90%",
      h: 4.5,
      title: slideData.title || "",
      titleFontSz: 18,
      titleFontColor: theme.primary,
      dataLabel: {
        labelValue: true,
        fontSize: 14,
        fontFace: "Microsoft YaHei",
      },
      legend: {
        position: "bottom",
        fontSize: 14,
        fontFace: "Microsoft YaHei",
      },
      border: { pt: 1, color: "#CCCCCC" },
    });
  }
}

// ─── 主函数 ─────────────────────────────────────────────────
async function main() {
  const args = parseArgs(process.argv);

  if (!THEMES[args.theme]) {
    console.error(`❌ 未知主题: ${args.theme}`);
    console.log("可用主题:", Object.keys(THEMES).join(", "));
    process.exit(1);
  }

  const theme = THEMES[args.theme];
  console.log(`📊 生成 PPT: "${args.title}"`);
  console.log(`🎨 主题: ${theme.name} (${theme.desc})`);
  console.log(`📄 幻灯片数: ${args.slides.length}`);
  console.log(`💾 输出: ${args.output}`);

  const prs = new PptxGenJS();
  prs.layout = "LAYOUT_16x9";

  // 字体设置由每处 fontFace 指定

  for (const slideData of args.slides) {
    switch (slideData.type) {
      case "title":
        addTitleSlide(prs, slideData, theme);
        break;
      case "content":
        addContentSlide(prs, slideData, theme);
        break;
      case "image":
        addImageSlide(prs, slideData, theme);
        break;
      case "table":
        addTableSlide(prs, slideData, theme);
        break;
      case "chart":
        addChartSlide(prs, slideData, theme);
        break;
      default:
        console.warn(`⚠️ 未知幻灯片类型: ${slideData.type}`);
    }
  }

  const outputDir = dirname(args.output);
  if (outputDir && outputDir !== ".") {
    mkdirSync(outputDir, { recursive: true });
  }

  await prs.writeFile({ fileName: args.output });
  console.log(`✅ PPT 已生成: ${args.output}`);
}

main().catch((err) => {
  console.error("❌ 生成失败:", err.message);
  process.exit(1);
});
