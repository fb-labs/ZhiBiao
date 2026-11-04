import { useEffect, useRef } from 'react';
import * as THREE from 'three';

type Props = {
  dense?: boolean;
};

/**
 * 招标文件 → 解读节点 → 标书章节 → 交付文件 的可视化场景。
 * 场景只作为背景叙事，不使用高饱和大面积色块。
 */
export function PipelineScene({ dense = false }: Props) {
  const host = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = host.current;
    if (!container) return;

    const width = container.clientWidth || 800;
    const height = container.clientHeight || 360;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(40, width / height, 0.1, 120);
    camera.position.set(0, 1.6, 19);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(width, height);
    renderer.domElement.style.width = '100%';
    renderer.domElement.style.height = '100%';
    container.appendChild(renderer.domElement);

    const group = new THREE.Group();
    group.position.set(dense ? 2.4 : 0.4, 0.2, 0);
    group.scale.setScalar(dense ? 0.92 : 1.2);
    scene.add(group);

    const colors = {
      indigo: 0x6366f1,
      violet: 0x8b5cf6,
      cyan: 0x22d3ee,
      teal: 0x0d9488,
      ink: 0x312e81,
    };

    const lineMaterial = (color: number, opacity: number) =>
      new THREE.LineBasicMaterial({ color, transparent: true, opacity, depthWrite: false });
    const fillMaterial = (color: number, opacity: number) =>
      new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity,
        side: THREE.DoubleSide,
        depthWrite: false,
      });

    // ── 招标文件：错落的纸张堆叠 ──
    const docs = new THREE.Group();
    docs.position.set(-6.9, -0.1, 0);
    docs.rotation.set(-0.08, 0.38, 0.02);
    const sheetGeo = new THREE.PlaneGeometry(3.1, 4.0);
    const sheetEdges = new THREE.EdgesGeometry(sheetGeo);
    for (let index = 0; index < 4; index += 1) {
      const sheet = new THREE.Mesh(
        sheetGeo,
        fillMaterial(index === 3 ? 0xe9e6ff : 0xdedaff, index === 3 ? 0.72 : 0.46),
      );
      sheet.position.set(index * 0.24, index * 0.16, -index * 0.06);
      sheet.rotation.set(0, 0, index * 0.02);
      docs.add(sheet);
      const edges = new THREE.LineSegments(sheetEdges, lineMaterial(colors.indigo, 0.65));
      edges.position.copy(sheet.position);
      edges.rotation.copy(sheet.rotation);
      docs.add(edges);
    }
    for (let line = 0; line < 7; line += 1) {
      const rule = new THREE.Mesh(
        new THREE.PlaneGeometry(line === 0 ? 1.3 : 2.1, 0.035),
        fillMaterial(colors.indigo, line === 0 ? 0.5 : 0.22),
      );
      rule.position.set(0.18, 1.35 - line * 0.38, 0.22);
      rule.rotation.z = 0.02;
      docs.add(rule);
    }
    group.add(docs);

    // ── 解读节点：环绕的评分项与风险点 ──
    const analysis = new THREE.Group();
    analysis.position.set(-2.5, 0.25, 0.25);
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(1.55, 0.028, 8, 140),
      lineMaterial(colors.cyan, 0.7),
    );
    ring.rotation.set(Math.PI / 2.35, 0.15, 0.25);
    analysis.add(ring);
    const ringInner = new THREE.Mesh(
      new THREE.TorusGeometry(1.05, 0.022, 8, 100),
      lineMaterial(colors.violet, 0.55),
    );
    ringInner.rotation.set(Math.PI / 2.1, -0.3, 0.1);
    analysis.add(ringInner);
    const core = new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.75, 0),
      lineMaterial(colors.indigo, 0.4),
    );
    analysis.add(core);
    const nodeGeo = new THREE.SphereGeometry(0.062, 12, 12);
    for (let index = 0; index < 9; index += 1) {
      const angle = (index / 9) * Math.PI * 2;
      const node = new THREE.Mesh(nodeGeo, fillMaterial(colors.indigo, 0.8));
      node.position.set(Math.cos(angle) * 1.55, Math.sin(angle) * 0.55, Math.sin(angle) * 1.2);
      analysis.add(node);
    }
    group.add(analysis);

    // ── 标书章节：结构化网格 ──
    const outline = new THREE.Group();
    outline.position.set(2.55, 0.35, 0.1);
    outline.rotation.set(-0.1, -0.28, 0);
    for (let row = 0; row < 3; row += 1) {
      for (let column = 0; column < 3; column += 1) {
        const cellGeo = new THREE.PlaneGeometry(0.92, 0.56);
        const cell = new THREE.Mesh(cellGeo, fillMaterial(0xffffff, 0.58));
        cell.position.set((column - 1) * 1.04, (row - 1) * 0.72, 0);
        outline.add(cell);
        const edges = new THREE.LineSegments(
          new THREE.EdgesGeometry(cellGeo),
          lineMaterial(row === 1 && column === 1 ? colors.violet : colors.indigo, 0.6),
        );
        edges.position.copy(cell.position);
        outline.add(edges);
        if (column < 2) {
          const link = new THREE.Mesh(
            new THREE.PlaneGeometry(0.14, 0.014),
            fillMaterial(colors.indigo, 0.3),
          );
          link.position.set((column - 0.5) * 1.04, (row - 1) * 0.72, 0.01);
          outline.add(link);
        }
      }
    }
    group.add(outline);

    // ── 交付文件：成稿与核验标记 ──
    const delivery = new THREE.Group();
    delivery.position.set(6.8, -0.15, 0.2);
    delivery.rotation.set(-0.06, -0.4, 0.02);
    const finalSheetGeo = new THREE.PlaneGeometry(2.7, 3.55);
    const finalSheet = new THREE.Mesh(finalSheetGeo, fillMaterial(0xece9ff, 0.75));
    delivery.add(finalSheet);
    delivery.add(
      new THREE.LineSegments(new THREE.EdgesGeometry(finalSheetGeo), lineMaterial(colors.violet, 0.7)),
    );
    for (let line = 0; line < 6; line += 1) {
      const rule = new THREE.Mesh(
        new THREE.PlaneGeometry(line === 0 ? 1.0 : 1.7, 0.03),
        fillMaterial(colors.indigo, line === 0 ? 0.45 : 0.18),
      );
      rule.position.set(0.2, 1.25 - line * 0.36, 0.01);
      delivery.add(rule);
    }
    const seal = new THREE.Mesh(
      new THREE.TorusGeometry(0.42, 0.03, 8, 60),
      lineMaterial(colors.teal, 0.65),
    );
    seal.position.set(0.78, -1.05, 0.02);
    delivery.add(seal);
    const check = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(0.55, -1.0, 0.03),
      new THREE.Vector3(0.74, -1.2, 0.03),
      new THREE.Vector3(1.02, -0.83, 0.03),
    ]);
    delivery.add(new THREE.Line(check, lineMaterial(colors.teal, 0.85)));
    group.add(delivery);

    // ── 基线：四个站点的水平参照 ──
    const baseline = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-9.1, -1.45, 0.35),
      new THREE.Vector3(9.1, -1.45, 0.35),
    ]);
    group.add(new THREE.Line(baseline, lineMaterial(colors.indigo, 0.2)));
    for (const stationX of [-6.9, -2.5, 2.55, 6.8]) {
      const tick = new THREE.Mesh(new THREE.SphereGeometry(0.045, 10, 10), fillMaterial(colors.indigo, 0.55));
      tick.position.set(stationX, -1.45, 0.36);
      group.add(tick);
    }

    // ── 数据流：从文件流向交付 ──
    const flow = new THREE.CatmullRomCurve3([
      new THREE.Vector3(-5.1, 0.15, 0.7),
      new THREE.Vector3(-2.5, 0.7, 0.4),
      new THREE.Vector3(0.1, 1.05, 0.15),
      new THREE.Vector3(2.6, 0.75, 0.3),
      new THREE.Vector3(5.0, 0.2, 0.7),
    ]);
    group.add(
      new THREE.Mesh(
        new THREE.TubeGeometry(flow, 120, 0.012, 4, false),
        lineMaterial(colors.indigo, 0.22),
      ),
    );

    const flowCount = 42;
    const positions = new Float32Array(flowCount * 3);
    const tint = new Float32Array(flowCount * 3);
    const offsets = new Float32Array(flowCount);
    const cyanColor = new THREE.Color(colors.cyan);
    const violetColor = new THREE.Color(colors.violet);
    for (let index = 0; index < flowCount; index += 1) {
      offsets[index] = index / flowCount;
      const color = cyanColor.clone().lerp(violetColor, index / flowCount);
      tint[index * 3] = color.r;
      tint[index * 3 + 1] = color.g;
      tint[index * 3 + 2] = color.b;
    }
    const flowGeo = new THREE.BufferGeometry();
    flowGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    flowGeo.setAttribute('color', new THREE.BufferAttribute(tint, 3));
    const flowPoints = new THREE.Points(
      flowGeo,
      new THREE.PointsMaterial({
        size: 0.09,
        vertexColors: true,
        transparent: true,
        opacity: 0.75,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        sizeAttenuation: true,
      }),
    );
    group.add(flowPoints);

    // ── 环境粒子：极轻的空气感 ──
    const dustCount = 70;
    const dustPositions = new Float32Array(dustCount * 3);
    for (let index = 0; index < dustCount; index += 1) {
      dustPositions[index * 3] = (Math.random() - 0.5) * 22;
      dustPositions[index * 3 + 1] = (Math.random() - 0.5) * 10;
      dustPositions[index * 3 + 2] = (Math.random() - 0.5) * 8;
    }
    const dustGeo = new THREE.BufferGeometry();
    dustGeo.setAttribute('position', new THREE.BufferAttribute(dustPositions, 3));
    scene.add(
      new THREE.Points(
        dustGeo,
        new THREE.PointsMaterial({
          size: 0.05,
          color: colors.indigo,
          transparent: true,
          opacity: 0.22,
          depthWrite: false,
          sizeAttenuation: true,
        }),
      ),
    );

    const pointer = { x: 0, y: 0 };
    const onPointerMove = (event: PointerEvent) => {
      pointer.x = (event.clientX / window.innerWidth - 0.5) * 0.8;
      pointer.y = (event.clientY / window.innerHeight - 0.5) * 0.5;
    };
    window.addEventListener('pointermove', onPointerMove);

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let frame = 0;
    let running = true;
    const clock = new THREE.Clock();

    const render = () => {
      const time = clock.getElapsedTime();
      camera.position.x = pointer.x;
      camera.position.y = 1.6 + pointer.y;
      camera.lookAt(0, 0.6, 0);

      docs.position.y = -0.1 + Math.sin(time * 0.9) * 0.06;
      analysis.rotation.y = Math.sin(time * 0.35) * 0.18 + 0.2;
      ring.rotation.z = time * 0.12;
      core.rotation.y = time * 0.3;
      core.rotation.x = time * 0.18;
      outline.position.y = 0.35 + Math.sin(time * 0.7 + 1.2) * 0.05;
      delivery.position.y = -0.15 + Math.sin(time * 0.8 + 2.1) * 0.07;

      const attr = flowGeo.getAttribute('position') as THREE.BufferAttribute;
      for (let index = 0; index < flowCount; index += 1) {
        const progress = (offsets[index] + time * 0.055) % 1;
        const point = flow.getPointAt(progress);
        attr.setXYZ(index, point.x, point.y, point.z);
      }
      attr.needsUpdate = true;

      renderer.render(scene, camera);
      if (running && !reduced) frame = window.requestAnimationFrame(render);
    };

    render();

    const resize = () => {
      const nextWidth = container.clientWidth || width;
      const nextHeight = container.clientHeight || height;
      renderer.setSize(nextWidth, nextHeight, false);
      camera.aspect = nextWidth / nextHeight;
      camera.updateProjectionMatrix();
      if (reduced) renderer.render(scene, camera);
    };
    const observer = new ResizeObserver(resize);
    observer.observe(container);

    return () => {
      running = false;
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      window.removeEventListener('pointermove', onPointerMove);
      renderer.dispose();
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh || object instanceof THREE.Line || object instanceof THREE.Points) {
          object.geometry.dispose();
          const material = object.material;
          if (Array.isArray(material)) material.forEach((item) => item.dispose());
          else material.dispose();
        }
      });
      if (renderer.domElement.parentNode === container) {
        container.removeChild(renderer.domElement);
      }
    };
  }, [dense]);

  return <div className="pipeline-scene" ref={host} aria-hidden="true" />;
}
